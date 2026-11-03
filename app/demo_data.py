"""Fictional demo-company data (P2-7/P2-8 shared source of truth).

Every fact is fictional sample-company data (Meridian Field
Services, a placeholder company). The eval script (scripts/eval.py)
and the demo seed script (scripts/seed_demo.py) both load exactly
these facts, so eval questions and demo beats always agree.

Each entry: (fact, visible_to, effective_date, doc_title).
"supersedes" links a newer fact to the older entry it replaces
(by list index); the older row is kept with status superseded.
Doc texts are one line per fact, so each document's content is
exactly the facts it carries.

Seeding opens one operator-scoped connection per fact: a
write_fact commit clears SET LOCAL, so sharing one scoped
connection across restricted writes breaks the second one (the
same RLS gotcha the test suites work around).
"""

from datetime import date

ALL_ROLES = ["employee", "cfo", "cto", "ceo"]
FINANCE = ["cfo", "ceo"]
TECHNICAL = ["cto", "ceo"]
EXECUTIVE = ["ceo"]

# (fact, visible_to, effective_date, doc_title, supersedes_index|None)
DEMO_FACTS = [
    ("Travel limit is 8000 rupees per trip.",
     ALL_ROLES, date(2026, 1, 1), "travel-policy.txt", None),
    ("Travel limit is 12000 rupees per trip.",
     ALL_ROLES, date(2026, 10, 1), "travel-policy-v2.txt", 0),
    ("Mobile allowance is 900 rupees per month.",
     ALL_ROLES, date(2026, 1, 1), "hr-policy.txt", None),
    ("Internet allowance is 1500 rupees per month.",
     ALL_ROLES, date(2026, 1, 1), "hr-policy.txt", None),
    ("Internet allowance is 1850 rupees per month.",
     ALL_ROLES, date(2026, 10, 1), "hr-policy-v2.txt", 3),
    ("Office opens at 9 am.",
     ALL_ROLES, date(2026, 1, 1), "hr-policy.txt", None),
    ("Medical reimbursement is up to 30000 rupees per year.",
     ALL_ROLES, date(2026, 1, 1), "hr-policy.txt", None),
    ("Salary is credited on the 27th of every month.",
     ALL_ROLES, date(2026, 1, 1), "hr-policy.txt", None),
    ("Local conveyance is 650 rupees per day.",
     ALL_ROLES, date(2026, 1, 1), "hr-policy.txt", None),
    ("Outstation cab is 9750 rupees per trip.",
     ALL_ROLES, date(2026, 1, 1), "hr-policy.txt", None),
    ("Tour food allowance is 1100 rupees per day.",
     ALL_ROLES, date(2026, 1, 1), "hr-policy.txt", None),
    ("Training allowance is 20000 rupees per year.",
     ALL_ROLES, date(2026, 1, 1), "hr-policy.txt", None),
    ("Casual leave is 12 days per year.",
     ALL_ROLES, date(2026, 1, 1), "leave-policy.txt", None),
    ("Sick leave is 12 days per year.",
     ALL_ROLES, date(2026, 1, 1), "leave-policy.txt", None),
    ("Earned leave is 24 days per year.",
     ALL_ROLES, date(2026, 1, 1), "leave-policy.txt", None),
    ("Quarterly budget is 500000 rupees.",
     FINANCE, date(2026, 1, 1), "finance-policy.txt", None),
    ("Reimbursement cap is 50000 rupees per claim.",
     FINANCE, date(2026, 1, 1), "finance-policy.txt", None),
    ("Salary advance is up to 40000 rupees once per quarter.",
     FINANCE, date(2026, 1, 1), "finance-policy.txt", None),
    ("Server migration deadline is 15 December 2026.",
     TECHNICAL, date(2026, 1, 1), "tech-policy.txt", None),
    ("Production deploy window is Sunday 2 am to 4 am.",
     TECHNICAL, date(2026, 1, 1), "tech-policy.txt", None),
    ("Board meeting is on the first Monday of every quarter.",
     EXECUTIVE, date(2026, 1, 1), "board-note.txt", None),
]

DOC_TITLES = sorted({entry[3] for entry in DEMO_FACTS})


def wipe_company(conn, company_id: str) -> None:
    """Delete one company's knowledge + gate rows (not audits).
    Gate tables are wiped only when they exist, so seeding works
    on a machine where a later migration was not applied yet."""
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    }
    conn.execute(
        "DELETE FROM memory_sources WHERE memory_id IN "
        "(SELECT id FROM memories WHERE company_id = %s)",
        (company_id,),
    )
    conn.execute(
        "DELETE FROM chunks WHERE document_id IN "
        "(SELECT id FROM documents WHERE company_id = %s)",
        (company_id,),
    )
    conn.execute("DELETE FROM documents WHERE company_id = %s", (company_id,))
    conn.execute("DELETE FROM memories WHERE company_id = %s", (company_id,))
    conn.execute("DELETE FROM edges WHERE company_id = %s", (company_id,))
    for table in (
        "engine_relations",
        "engine_entities",
        "engine_uploads",
        "pending_access_requests",
        "pending_conflicts",
        "pending_questions",
    ):
        if table in existing:
            conn.execute(
                f"DELETE FROM {table} WHERE company_id = %s", (company_id,)
            )
    conn.commit()


def seed_company(company_id: str) -> dict:
    """Wipe + load DEMO_FACTS into a company. Returns counts.

    Two phases: documents first on a plain connection (documents
    carry no RLS), then one operator-scoped connection per fact.
    ingest_document commits internally, which clears SET LOCAL —
    so ingesting and writing on the same scoped connection would
    break restricted writes (the RLS gotcha again)."""
    from app.db import get_conn, scoped_conn
    from app.store import ingest_document, write_fact

    # Wipe operator-scoped: a plain connection cannot DELETE rows
    # RLS hides from it, and write_fact dedup would then reuse
    # the stale survivors (including link-less ones).
    with scoped_conn("ceo", unlocked=True) as conn:
        wipe_company(conn, company_id)
    grouped = {}
    for fact, _vis, _eff, doc_title, _sup in DEMO_FACTS:
        grouped.setdefault(doc_title, []).append(fact)
    doc_ids = {}
    with get_conn() as conn:
        for doc_title, facts in grouped.items():
            # custom_id is global in ingest_document, so scope it
            # per company — otherwise companies share document rows
            # and a fresh company ends up with zero documents.
            doc_ids[doc_title] = ingest_document(
                conn, company_id, f"doc:{company_id}:{doc_title}",
                doc_title, "\n".join(facts),
            )
        conn.commit()
    fact_ids = []
    for fact, visible_to, eff, doc_title, supersedes_idx in DEMO_FACTS:
        with scoped_conn("ceo", unlocked=True) as conn:
            supersedes_id = (
                fact_ids[supersedes_idx]
                if supersedes_idx is not None
                else None
            )
            fid = write_fact(
                conn, company_id, fact, visible_to=visible_to,
                effective_date=eff, source="document",
                document_id=doc_ids[doc_title],
                supersedes_id=supersedes_id,
            )
            fact_ids.append(fid)
    return {"facts": len(fact_ids), "documents": len(doc_ids)}
