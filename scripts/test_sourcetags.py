"""Targeted proofs for source tags + exact not-found wording (P0-3).

Prerequisites: base schemas + schema_security.sql applied,
DATABASE_URL set. Synthesis is patched (no model key here), so
the router path is real and only the model text is simulated.

Seeds (company "tags-co", legacy facts only):
- travel limit 8000 (travel-policy.txt, 2026-01-01) superseded by
  travel limit 12000 (travel-policy-v2.txt, 2026-10-01),
- mobile allowance 900/month, single version (hr-policy.txt).

Checks:
(a) an ENGINE answer with facts gains a Sources block: doc name,
    date, and (current) tag.
(b) an out-of-scope question returns EXACTLY
    "Not found in company documents."
(c) an as-of answer is NOT double-tagged (no Sources block).
(d) an empty (no-key) ENGINE answer gains no tags.
(e) source_lines follows stored status: a superseded fact tags
    (superseded), not (current).
(f) simulated WhatsApp round-trip: synthesis + Sources block.

Run: PYTHONPATH=. python scripts/test_sourcetags.py
Exit code 0 iff every check passes. Cleans its own company rows first.
"""

import os
import sys
import tempfile
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import sourcetags as tags_mod
from app.db import get_conn
from app.engine import router
from app.store import ingest_document, write_fact

COMPANY = "tags-co"
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

        import app.llm as llm_mod

        real_synth = llm_mod.synthesize

        def _fake_synth(question, facts, conflicts):
            return "The travel limit is 12000 rupees per trip."

        llm_mod.synthesize = _fake_synth
        try:
            # (a) ENGINE answer gains a Sources block.
            ans_a = router.answer_question(
                conn, COMPANY, "employee", "What is the travel limit?"
            )
            text_a = ans_a.get("answer") or ""
            check(
                "a-sources-block",
                ans_a["route"] == "ENGINE"
                and "The travel limit is 12000" in text_a
                and "Sources:" in text_a
                and "travel-policy-v2.txt" in text_a
                and "2026-10-01" in text_a
                and "(current)" in text_a,
                f"answer={text_a}",
            )

            # (c) As-of answer is not double-tagged.
            ans_c = router.answer_question(
                conn, COMPANY, "employee",
                "What was the travel limit before?",
            )
            text_c = ans_c.get("answer") or ""
            check(
                "c-asof-not-double-tagged",
                "Sources:" not in text_c
                and "(current)" in text_c
                and "(superseded)" in text_c,
                f"answer={text_c[:120]}",
            )

            # (f) Simulated WhatsApp round-trip with tags.
            import app.whatsapp as wa_mod

            os.environ["COMPANY_ID"] = COMPANY
            os.environ["WHATSAPP_TOKEN"] = "dummy-token"
            os.environ["WHATSAPP_PHONE_ID"] = "12345"
            os.environ["MEDIA_DIR"] = tempfile.mkdtemp(prefix="wa_tags_")
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
                                    "from": "910000000301",
                                    "id": "wamid-tags-1",
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": "What is the travel limit?"},
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
                    "f-webhook-sources",
                    resp.status_code == 200
                    and "The travel limit is 12000" in body
                    and "Sources:" in body
                    and "travel-policy-v2.txt" in body
                    and "2026-10-01" in body
                    and "(current)" in body,
                    f"body={body[:200]}",
                )
            finally:
                wa_mod._deliver = real_deliver
                os.environ.pop("WHATSAPP_TOKEN", None)
                os.environ.pop("WHATSAPP_PHONE_ID", None)
        finally:
            llm_mod.synthesize = real_synth

        # (b) Out-of-scope wording is exact.
        ans_b = router.answer_question(
            conn, COMPANY, "employee",
            "What is the stock option vesting schedule?",
        )
        check(
            "b-not-found-exact",
            ans_b["route"] == "NOT_FOUND"
            and ans_b.get("answer") == "Not found in company documents.",
            f"route={ans_b['route']} answer={ans_b.get('answer')}",
        )

        # (d) Empty ENGINE answer gains no tags.
        ans_d = router.answer_question(
            conn, COMPANY, "employee", "What is the mobile allowance?"
        )
        check(
            "d-empty-untagged",
            ans_d["route"] == "ENGINE"
            and (ans_d.get("answer") or "") == ""
            and "Sources:" not in (ans_d.get("answer") or ""),
            f"route={ans_d['route']} answer={ans_d.get('answer')!r}",
        )

        # (e) Superseded fact tags (superseded).
        lines = tags_mod.source_lines(conn, COMPANY, [{"id": old_id}])
        check(
            "e-superseded-tag",
            len(lines) == 1
            and "travel-policy.txt" in lines[0]
            and "2026-01-01" in lines[0]
            and "(superseded)" in lines[0],
            f"lines={lines}",
        )
        cleanup(conn)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
