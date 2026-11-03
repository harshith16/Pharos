"""Gate for the demo seed script (P2-8).

Prerequisites: base schemas applied, DATABASE_URL set.

Checks:
(a) seed_demo.py runs clean (exit 0) for a fresh company.
(b) Re-running it yields identical counts (idempotent re-record):
    21 facts (19 current, 2 superseded), 8 documents.
(c) Both superseded pairs exist with their links: travel 8000
    superseded by 12000, internet 1500 superseded by 1850.
(d) FORCE ROW LEVEL SECURITY is on for the four sensitive
    tables after the schema re-apply.
(e) Another company's rows are untouched by the seed.
(f) Simulated WhatsApp round-trip on the seeded state: the
    as-of question returns both values with tags (no model key
    needed — as-of formatting is deterministic).

Run: PYTHONPATH=. python scripts/test_seed_demo.py
Exit code 0 iff every check passes.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_conn, scoped_conn
from scripts import seed_demo

COMPANY = "seed-test-co"
OTHER = "eval-co"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))


def _counts(company_id):
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
        return total, current, superseded, docs


def main():
    # Scoped count: a plain connection cannot see restricted rows.
    with scoped_conn("ceo", unlocked=True) as conn:
        other_before = conn.execute(
            "SELECT count(*) FROM memories WHERE company_id = %s",
            (OTHER,),
        ).fetchone()[0]

    # (a)+(b) Two runs, both clean, identical counts.
    rc1 = seed_demo.main(["--company", COMPANY])
    counts1 = _counts(COMPANY)
    rc2 = seed_demo.main(["--company", COMPANY])
    counts2 = _counts(COMPANY)
    check(
        "a-runs-clean",
        rc1 == 0 and rc2 == 0,
        f"rc1={rc1} rc2={rc2}",
    )
    check(
        "b-idempotent-counts",
        counts1 == (21, 19, 2, 8) and counts2 == (21, 19, 2, 8),
        f"run1={counts1} run2={counts2}",
    )

    # (c) Superseded pairs with links.
    with scoped_conn("ceo", unlocked=True) as conn:
        pairs = conn.execute(
            "SELECT old.fact, new.fact FROM memories new "
            "JOIN memories old ON old.id = new.supersedes_id "
            "WHERE new.company_id = %s AND new.status = 'current' "
            "AND old.status = 'superseded'",
            (COMPANY,),
        ).fetchall()
    pair_texts = {(o, n) for o, n in pairs}
    check(
        "c-superseded-pairs",
        ("Travel limit is 8000 rupees per trip.",
         "Travel limit is 12000 rupees per trip.") in pair_texts
        and ("Internet allowance is 1500 rupees per month.",
             "Internet allowance is 1850 rupees per month.") in pair_texts,
        f"pairs={pair_texts}",
    )

    # (d) FORCE RLS on the four sensitive tables.
    with get_conn() as conn:
        forced = {
            row[0]
            for row in conn.execute(
                "SELECT relname FROM pg_class WHERE relname = ANY(%s) "
                "AND relforcerowsecurity",
                (seed_demo.RLS_TABLES,),
            ).fetchall()
        }
    check(
        "d-force-rls",
        set(seed_demo.RLS_TABLES) <= forced,
        f"forced={sorted(forced)}",
    )

    # (e) Other company untouched.
    with scoped_conn("ceo", unlocked=True) as conn:
        other_after = conn.execute(
            "SELECT count(*) FROM memories WHERE company_id = %s",
            (OTHER,),
        ).fetchone()[0]
    check(
        "e-other-company-untouched",
        other_before == other_after,
        f"before={other_before} after={other_after}",
    )

    # (f) Simulated WhatsApp round-trip on the seeded state.
    import tempfile
    import time

    import app.whatsapp as wa_mod

    real_deliver = wa_mod._deliver
    sent_bodies = []

    async def _fake_deliver(client, phone_id, token, sender_to, body):
        sent_bodies.append(body)
        return "sent", "fake-msg-id"

    wa_mod._deliver = _fake_deliver
    try:
        from fastapi.testclient import TestClient

        from app.main import app

        os.environ["COMPANY_ID"] = COMPANY
        os.environ["WHATSAPP_TOKEN"] = "dummy-token"
        os.environ["WHATSAPP_PHONE_ID"] = "12345"
        os.environ["MEDIA_DIR"] = tempfile.mkdtemp(prefix="wa_seed_")
        client = TestClient(app)
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "messages": [{
                            "from": "910000000601",
                            "id": "wamid-seed-1",
                            "timestamp": "1700000000",
                            "type": "text",
                            "text": {"body": "What was the travel limit before?"},
                        }]
                    }
                }]
            }]
        }
        resp = client.post("/webhook/whatsapp", json=payload)
        body = ""
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if sent_bodies:
                body = sent_bodies[-1]
                break
            time.sleep(0.2)
        check(
            "f-webhook-seeded-asof",
            resp.status_code == 200
            and "12000" in body
            and "8000" in body
            and "(current)" in body
            and "(superseded)" in body,
            f"body={body[:160]}",
        )
    finally:
        wa_mod._deliver = real_deliver
        os.environ.pop("WHATSAPP_TOKEN", None)
        os.environ.pop("WHATSAPP_PHONE_ID", None)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
