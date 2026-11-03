"""Targeted proofs for as-of answers (P0-2).

Prerequisites: base schemas + schema_security.sql applied,
DATABASE_URL set. No model key needed: as-of formatting is
deterministic from stored version chains.

Seeds (company "asof-co", legacy facts only, so the router takes
its legacy ENGINE path):
- travel limit 8000 (travel-policy.txt, 2026-01-01) superseded by
  travel limit 12000 (travel-policy-v2.txt, 2026-10-01),
- mobile allowance 900/month, single version (hr-policy.txt).

Checks (router.answer_question, real DB):
(a) "what was the travel limit before?" -> BOTH values, each with
    source doc, date, and (current)/(superseded) tag.
(b) "what is the travel limit now?" -> same both-values answer.
(c) single-version as-of question -> that value tagged current +
    "No earlier value was found.", nothing invented.
(d) a normal (non-as-of) question is NOT as-of formatted.
(e) is_asof_question markers: true for as-of phrasing, false for
    plain questions.
(f) simulated WhatsApp round-trip: the as-of question over the
    real webhook handler returns both values with tags.

Run: PYTHONPATH=. python scripts/test_asof.py
Exit code 0 iff every check passes. Cleans its own company rows first.
"""

import os
import sys
import tempfile
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import asof as asof_mod
from app.db import get_conn
from app.engine import router
from app.store import ingest_document, write_fact

COMPANY = "asof-co"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))


def cleanup(conn):
    conn.execute(
        "DELETE FROM chunks WHERE document_id IN "
        "(SELECT id FROM documents WHERE company_id = %s)",
        (COMPANY,),
    )
    conn.execute("DELETE FROM documents WHERE company_id = %s", (COMPANY,))
    conn.execute(
        "DELETE FROM memory_sources WHERE memory_id IN "
        "(SELECT id FROM memories WHERE company_id = %s)",
        (COMPANY,),
    )
    conn.execute("DELETE FROM memories WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM edges WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM engine_relations WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM engine_entities WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM engine_uploads WHERE company_id = %s", (COMPANY,))
    conn.commit()


def main():
    with get_conn() as conn:
        cleanup(conn)
        doc1 = ingest_document(
            conn, COMPANY, "doc:travel-v1", "travel-policy.txt",
            "Travel limit policy v1.",
        )
        old_id = write_fact(
            conn, COMPANY, "Travel limit is 8000 rupees per trip.",
            visible_to=["employee", "cfo", "cto", "ceo"],
            effective_date=date(2026, 1, 1), source="document",
            document_id=doc1,
        )
        doc2 = ingest_document(
            conn, COMPANY, "doc:travel-v2", "travel-policy-v2.txt",
            "Travel limit policy v2.",
        )
        write_fact(
            conn, COMPANY, "Travel limit is 12000 rupees per trip.",
            visible_to=["employee", "cfo", "cto", "ceo"],
            effective_date=date(2026, 10, 1), source="document",
            document_id=doc2, supersedes_id=old_id,
        )
        doc3 = ingest_document(
            conn, COMPANY, "doc:hr", "hr-policy.txt", "HR policy.",
        )
        write_fact(
            conn, COMPANY, "Mobile allowance is 900 rupees per month.",
            visible_to=["employee", "cfo", "cto", "ceo"],
            effective_date=date(2026, 1, 1), source="document",
            document_id=doc3,
        )
        conn.commit()

        # (a) "what was ... before?" -> both values with tags.
        ans_a = router.answer_question(
            conn, COMPANY, "employee", "What was the travel limit before?"
        )
        text_a = ans_a.get("answer") or ""
        check(
            "a-before-both-values",
            ans_a["route"] == "ENGINE"
            and "12000" in text_a
            and "8000" in text_a
            and "(current)" in text_a
            and "(superseded)" in text_a
            and "travel-policy-v2.txt" in text_a
            and "travel-policy.txt" in text_a
            and "2026-10-01" in text_a
            and "2026-01-01" in text_a
            and text_a.index("12000") < text_a.index("8000"),
            f"answer={text_a}",
        )

        # (b) "what is ... now?" -> same both-values answer.
        ans_b = router.answer_question(
            conn, COMPANY, "employee", "What is the travel limit now?"
        )
        text_b = ans_b.get("answer") or ""
        check(
            "b-now-both-values",
            ans_b["route"] == "ENGINE"
            and "12000" in text_b
            and "8000" in text_b
            and "(current)" in text_b
            and "(superseded)" in text_b,
            f"answer={text_b}",
        )

        # (c) Single version -> current tag + plain no-earlier note.
        ans_c = router.answer_question(
            conn, COMPANY, "employee",
            "What was the mobile allowance before?",
        )
        text_c = ans_c.get("answer") or ""
        check(
            "c-single-current-plus-note",
            ans_c["route"] == "ENGINE"
            and "900" in text_c
            and "(current)" in text_c
            and "(superseded)" not in text_c
            and "No earlier value was found." in text_c
            and text_c.count("•") == 1,
            f"answer={text_c}",
        )

        # (d) Normal question is NOT as-of formatted.
        ans_d = router.answer_question(
            conn, COMPANY, "employee", "What is the travel limit?"
        )
        text_d = ans_d.get("answer") or ""
        check(
            "d-normal-not-asof",
            ans_d["route"] == "ENGINE"
            and "(superseded)" not in text_d
            and "No earlier value was found." not in text_d,
            f"answer={text_d[:80]}",
        )

        # (e) Marker detection.
        check(
            "e-marker-detection",
            asof_mod.is_asof_question("What was X before?")
            and asof_mod.is_asof_question("What is X now?")
            and asof_mod.is_asof_question("What did X previously cost?")
            and asof_mod.is_asof_question("What is the current X?")
            and not asof_mod.is_asof_question("What is the travel limit?")
            and not asof_mod.is_asof_question(
                "What is the Champions League schedule?"
            ),
            "markers ok",
        )

        # (f) Simulated WhatsApp round-trip.
        import app.whatsapp as wa_mod

        os.environ["COMPANY_ID"] = COMPANY
        os.environ["WHATSAPP_TOKEN"] = "dummy-token"
        os.environ["WHATSAPP_PHONE_ID"] = "12345"
        os.environ["MEDIA_DIR"] = tempfile.mkdtemp(prefix="wa_asof_")
        real_deliver = wa_mod._deliver
        sent_bodies = []

        async def _fake_deliver(client, phone_id, token, sender_to, body):
            sent_bodies.append(body)
            return "sent", "fake-msg-id"

        wa_mod._deliver = _fake_deliver
        try:
            from fastapi.testclient import TestClient

            from app.main import app

            client = TestClient(app)
            payload = {
                "entry": [{
                    "changes": [{
                        "value": {
                            "messages": [{
                                "from": "910000000201",
                                "id": "wamid-asof-1",
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
                "f-webhook-asof",
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
        cleanup(conn)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
