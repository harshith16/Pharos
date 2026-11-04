"""Demo seed script: one command, clean fictional-company state.

Command:
    PYTHONPATH=. python scripts/seed_demo.py [--company <id>]

What it does, in order:
1. Re-applies every schema in order (all idempotent):
   schema.sql, schema_engine.sql, schema_media.sql,
   schema_actions.sql, schema_security.sql, schema_conflicts.sql,
   schema_access.sql.
2. Wipes ONLY the target company (default: config.COMPANY_ID,
   else "demo-co") — knowledge + gate rows, never other
   companies, never audit rows. Re-running is safe: the same
   21 facts and 8 documents every time.
3. Loads the fictional Meridian Field Services facts from
   app.demo_data: 15 general,
   3 finance (CFO+CEO), 2 technical (CTO+CEO), 1 executive
   (CEO-only), including two superseded pairs for as-of
   answers (travel 8000 -> 12000; internet 1500 -> 1850).
4. Verifies: fact/document counts, superseded pairs, FORCE RLS
   on the four sensitive tables. Warns loudly if the DB role
   is superuser or BYPASSRLS — RLS is theater then, and the
   printed ALTER ROLE must run as a superuser first.
5. Prints the demo beats this state supports, in order.

Fictional data only. The conflict/alert beat, the receipt
beat, the code-gate beat, and the ACT beat are demonstrated
live on top of this state (upload / photo / question), not
pre-faked: the seed provides the stored facts those flows act
on. The brand-new-upload memo beat (overnight stay allowance
6850, approver Head of Operations) is also a live upload, not
seeded — seeding it would spoil the new-upload demonstration.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, demo_data
from app.db import get_conn, scoped_conn

SCHEMA_FILES = [
    "schema.sql",
    "schema_engine.sql",
    "schema_media.sql",
    "schema_actions.sql",
    "schema_security.sql",
    "schema_conflicts.sql",
    "schema_access.sql",
]

RLS_TABLES = ["memories", "engine_relations", "edges", "engine_entities"]

DEMO_BEATS = """
Demo beats this state supports (in order):
1. General Q&A — "What is the travel limit?"
   -> 12000 rupees per trip + Sources (travel-policy-v2.txt).
2. As-of — "What was the travel limit before?"
   -> BOTH 12000 (current) and 8000 (superseded), each with doc
   and date. Also: "What is the internet allowance now?"
   -> BOTH 1850 (current) and 1500 (superseded).
3. Conflict alert — upload a document saying the travel limit
   is 15000 rupees per trip.
   -> "New doc says ... Stored fact (travel-policy-v2.txt,
   2026-10-01) says ... Replace? Reply YES/NO." YES replaces
   (old kept, superseded); NO discards the new one.
4. Role-block — as an employee, "What is the quarterly budget?"
   -> "Restricted to Finance. Request access from CFO?"
   YES sends the request to the CFO number (configure
   WHATSAPP_CFO_PHONE first) or says plainly it is not
   configured.
5. Code gate — as CFO, "What is the quarterly budget?"
   -> code challenge -> 8-digit code -> answer under the role
   filter. Set the code once first: scripts/set_auth_code.py.
6. Receipt — send a receipt photo captioned "receipt".
   -> vendor/amount/date emailed to HR (Gmail must be connected
   at /auth/gmail and the recipient set on the connect page),
   WhatsApp confirmation, audit row.
7. Action — "I want reimbursement, inform HR"
   -> HR emailed via the Gmail connect flow, audit row.
8. Out-of-scope — "What is the stock option vesting schedule?"
   -> exactly "Not found in company documents."
"""


def _apply_schemas(repo_root):
    with get_conn() as conn:
        for name in SCHEMA_FILES:
            path = os.path.join(repo_root, name)
            if not os.path.isfile(path):
                print(f"seed_demo: missing schema file: {path}")
                return False
            with open(path, encoding="utf-8") as fh:
                conn.execute(fh.read())
            conn.commit()
            print(f"seed_demo: applied {name}")
    return True


def _verify(company_id):
    """Counts + RLS state. Returns (ok, warnings list)."""
    warnings = []
    with scoped_conn("ceo", unlocked=True) as conn:
        total = conn.execute(
            "SELECT count(*) FROM memories WHERE company_id = %s",
            (company_id,),
        ).fetchone()[0]
        current = conn.execute(
            "SELECT count(*) FROM memories WHERE company_id = %s "
            "AND status = 'current'",
            (company_id,),
        ).fetchone()[0]
        superseded = conn.execute(
            "SELECT count(*) FROM memories WHERE company_id = %s "
            "AND status = 'superseded'",
            (company_id,),
        ).fetchone()[0]
        docs = conn.execute(
            "SELECT count(*) FROM documents WHERE company_id = %s",
            (company_id,),
        ).fetchone()[0]
        forced = {
            row[0]
            for row in conn.execute(
                "SELECT relname FROM pg_class "
                "WHERE relname = ANY(%s) AND relforcerowsecurity",
                (RLS_TABLES,),
            ).fetchall()
        }
        role_row = conn.execute(
            "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
            "WHERE rolname = current_user"
        ).fetchone()
    print(
        f"seed_demo: facts={total} (current={current}, "
        f"superseded={superseded}) documents={docs}"
    )
    if total != 21 or docs != 8 or superseded != 2:
        warnings.append(
            f"unexpected counts: facts={total} documents={docs} "
            f"superseded={superseded} (want 21 / 8 / 2)"
        )
    missing_force = [t for t in RLS_TABLES if t not in forced]
    if missing_force:
        warnings.append(
            f"FORCE ROW LEVEL SECURITY missing on: {missing_force}"
        )
    if role_row is not None:
        rolname, rolsuper, rolbypassrls = role_row
        if rolsuper or rolbypassrls:
            warnings.append(
                f"DB role {rolname} is superuser={rolsuper} "
                f"bypassrls={rolbypassrls}: RLS does NOT apply to it. "
                f"As a superuser run: ALTER ROLE {rolname} "
                "NOSUPERUSER NOBYPASSRLS; then re-run this seed."
            )
    return (not warnings, warnings)


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--company",
        default=config.COMPANY_ID or "demo-co",
        help="Company to reset and seed (default: config COMPANY_ID "
        "or demo-co). Only this company is wiped.",
    )
    args = parser.parse_args(argv)
    print(f"seed_demo: company={args.company} (only this company wiped)")
    if not _apply_schemas(repo_root):
        return 2
    counts = demo_data.seed_company(args.company)
    print(
        f"seed_demo: seeded {counts['facts']} facts, "
        f"{counts['documents']} documents into {args.company}"
    )
    ok, warnings = _verify(args.company)
    for warning in warnings:
        print(f"seed_demo WARNING: {warning}")
    print(DEMO_BEATS)
    if not ok:
        print("seed_demo: verification warnings above; state may be stale.")
        return 1
    print("seed_demo: done — clean fictional-company state ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
