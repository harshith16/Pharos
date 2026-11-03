"""Targeted proofs for upload conflict alerts (P0-1).

Prerequisites: base schemas + schema_security.sql +
schema_conflicts.sql applied, DATABASE_URL set.

Seeds (company "conflicts-co"): a travel-limit fact (8000, from a
document titled travel-policy.txt, effective 2026-01-01) with a
limit triple, so a new 12000 limit contradicts it.

Checks (module level, real DB):
(a) find_conflicts detects the contradiction, ignores a same-value
    fact and an unrelated-relationship fact.
(b) store_facts_checked holds the conflicting fact (nothing stored)
    and returns the exact founder alert shape.
(c) YES resolves: new fact active, old superseded (kept), pending
    resolved_yes.
(d) NO resolves: new fact never stored, old stays latest, pending
    resolved_no.
(e) Queue: a second conflict while one is pending queues; resolving
    the first promotes it and returns its alert.

Checks (simulated WhatsApp round-trip, real webhook handler):
(f) POST a document message (patched Graph download + patched
    fact extraction) -> the uploader receives the exact alert
    shape; POST "YES" -> resolution reply, and the DB shows the
    new fact active and the old superseded.

Run: PYTHONPATH=. python scripts/test_conflicts.py
Exit code 0 iff every check passes. Cleans its own company rows first.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import conflicts
from app.db import get_conn, scoped_conn
from app.store import ingest_document, write_fact

COMPANY = "conflicts-co"
RESULTS = []

OLD_FACT = "Travel limit is 8000 rupees per trip."
NEW_FACT = "Travel limit is 12000 rupees per trip."
NEW_FACT_2 = "Travel limit is 15000 rupees per trip."


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
    conn.execute(
        "DELETE FROM pending_conflicts WHERE company_id = %s", (COMPANY,)
    )
    conn.commit()


def _fact_dict(fact, dest):
    return {
        "fact": fact,
        "visible_to": ["employee", "cfo", "cto", "ceo"],
        "effective_date": None,
        "triples": [
            {
                "source": "travel_policy",
                "relationship": "limit",
                "destination": dest,
            }
        ],
    }


def main():
    from datetime import date

    sender = "910000000101"
    with get_conn() as conn:
        cleanup(conn)
        with scoped_conn("ceo", unlocked=True) as sc:
            doc_id = ingest_document(
                sc, COMPANY, "doc:travel-policy", "travel-policy.txt",
                "Travel limit policy document.",
            )
            old_id = write_fact(
                sc, COMPANY, OLD_FACT,
                visible_to=["employee", "cfo", "cto", "ceo"],
                effective_date=date(2026, 1, 1),
                source="document",
                document_id=doc_id,
                triples=[{"source": "travel_policy", "relationship": "limit",
                          "destination": "8000"}],
            )
        conn.commit()

        with scoped_conn("ceo", unlocked=True) as sc:
            # (a) Detection.
            found = conflicts.find_conflicts(sc, COMPANY, _fact_dict(NEW_FACT, "12000"))
            same = conflicts.find_conflicts(sc, COMPANY, _fact_dict(OLD_FACT, "8000"))
            other = conflicts.find_conflicts(
                sc, COMPANY,
                {"fact": "Travel policy is managed by Priya.",
                 "triples": [{"source": "travel_policy",
                              "relationship": "managed_by",
                              "destination": "priya"}]},
            )
        check(
            "a-detects-contradiction-only",
            len(found) == 1
            and found[0]["memory_id"] == old_id
            and found[0]["fact"] == OLD_FACT
            and same == []
            and other == [],
            f"found={len(found)} same={len(same)} other={len(other)}",
        )

        # (b) Hold + exact alert shape.
        with scoped_conn("ceo", unlocked=True) as sc:
            stored, alert, queued = conflicts.store_facts_checked(
                sc, COMPANY, sender, NEW_FACT, "new-policy.txt", doc_id,
                "document", [_fact_dict(NEW_FACT, "12000")],
            )
            new_row = sc.execute(
                "SELECT id FROM memories WHERE company_id = %s AND fact = %s",
                (COMPANY, NEW_FACT),
            ).fetchone()
        expected_alert = (
            "New doc says Travel limit is 12000 rupees per trip. "
            "Stored fact (travel-policy.txt, 2026-01-01) says "
            "Travel limit is 8000 rupees per trip. Replace? Reply YES/NO."
        )
        check(
            "b-held-with-exact-alert",
            stored == []
            and queued == 0
            and alert == expected_alert
            and new_row is None,
            f"alert={alert}",
        )

        # (c) YES resolves: new active, old superseded + kept.
        reply_c, next_c = conflicts.resolve_conflict(COMPANY, sender, True)
        with get_conn() as c2:
            old_row = c2.execute(
                "SELECT is_latest, status FROM memories WHERE id = %s",
                (old_id,),
            ).fetchone()
            new_row = c2.execute(
                "SELECT is_latest, status, supersedes_id FROM memories "
                "WHERE company_id = %s AND fact = %s",
                (COMPANY, NEW_FACT),
            ).fetchone()
            pend = c2.execute(
                "SELECT status FROM pending_conflicts WHERE company_id = %s "
                "AND sender = %s ORDER BY created_at DESC LIMIT 1",
                (COMPANY, sender),
            ).fetchone()
        check(
            "c-yes-replaces-keeps-old",
            reply_c == conflicts.RESOLVED_YES_REPLY
            and next_c is None
            and old_row == (False, "superseded")
            and new_row is not None
            and new_row[0] is True
            and new_row[1] == "current"
            and str(new_row[2]) == old_id
            and pend is not None
            and pend[0] == "resolved_yes",
            f"reply={reply_c} old={old_row} new={new_row} pend={pend}",
        )

        # (d) NO resolves: nothing stored, old stays latest.
        with scoped_conn("ceo", unlocked=True) as sc:
            stored_d, alert_d, _q = conflicts.store_facts_checked(
                sc, COMPANY, sender, NEW_FACT_2, "new-policy-2.txt", doc_id,
                "document", [_fact_dict(NEW_FACT_2, "15000")],
            )
        reply_d, _n = conflicts.resolve_conflict(COMPANY, sender, False)
        with get_conn() as c2:
            latest = c2.execute(
                "SELECT fact, is_latest FROM memories WHERE company_id = %s "
                "AND is_latest AND fact LIKE 'Travel limit%%'",
                (COMPANY,),
            ).fetchall()
            no_row = c2.execute(
                "SELECT id FROM memories WHERE company_id = %s AND fact = %s",
                (COMPANY, NEW_FACT_2),
            ).fetchone()
        check(
            "d-no-discards-keeps-old",
            alert_d is not None
            and reply_d == conflicts.RESOLVED_NO_REPLY
            and no_row is None
            and len(latest) == 1
            and latest[0][0] == NEW_FACT,
            f"reply={reply_d} latest={latest}",
        )

        # (e) Queue: pending + queued, resolve promotes with alert.
        with scoped_conn("ceo", unlocked=True) as sc:
            _s1, alert_e1, _ = conflicts.store_facts_checked(
                sc, COMPANY, sender, NEW_FACT_2, "q1.txt", doc_id,
                "document", [_fact_dict(NEW_FACT_2, "15000")],
            )
            _s2, alert_e2, queued_e = conflicts.store_facts_checked(
                sc, COMPANY, sender, "Travel limit is 20000 rupees per trip.",
                "q2.txt", doc_id, "document",
                [_fact_dict("Travel limit is 20000 rupees per trip.", "20000")],
            )
        reply_e, next_e = conflicts.resolve_conflict(COMPANY, sender, False)
        check(
            "e-queue-promotes-on-resolve",
            alert_e1 is not None
            and alert_e2 is None
            and queued_e == 1
            and reply_e == conflicts.RESOLVED_NO_REPLY
            and next_e is not None
            and next_e.startswith("New doc says Travel limit is 20000")
            and next_e.endswith("Replace? Reply YES/NO."),
            f"alert1={bool(alert_e1)} queued={queued_e} next={next_e}",
        )
        # Resolve the promoted one so no pending state leaks.
        conflicts.resolve_conflict(COMPANY, sender, False)

        # (f) Simulated WhatsApp round-trip.
        import app.llm as llm_mod
        import app.whatsapp as wa_mod

        os.environ["COMPANY_ID"] = COMPANY
        os.environ["WHATSAPP_TOKEN"] = "dummy-token"
        os.environ["WHATSAPP_PHONE_ID"] = "12345"
        os.environ["MEDIA_DIR"] = tempfile.mkdtemp(prefix="wa_conf_")
        real_download = wa_mod._download_media
        real_deliver = wa_mod._deliver
        real_extract = llm_mod.extract_facts
        sent_bodies = []

        async def _fake_download(token, media_id):
            return b"Travel limit is 25000 rupees per trip.", "text/plain"

        async def _fake_deliver(client, phone_id, token, sender_to, body):
            sent_bodies.append(body)
            return "sent", "fake-msg-id"

        wa_mod._download_media = _fake_download
        wa_mod._deliver = _fake_deliver
        llm_mod.extract_facts = lambda text: [
            _fact_dict("Travel limit is 25000 rupees per trip.", "25000")
        ]
        try:
            from fastapi.testclient import TestClient

            from app.main import app

            client = TestClient(app)
            doc_payload = {
                "entry": [{
                    "changes": [{
                        "value": {
                            "messages": [{
                                "from": sender,
                                "id": "wamid-conf-1",
                                "timestamp": "1700000000",
                                "type": "document",
                                "document": {
                                    "id": "media-conf-1",
                                    "filename": "policy25.txt",
                                    "mime_type": "text/plain",
                                    "caption": None,
                                },
                            }]
                        }
                    }]
                }]
            }
            resp = client.post("/webhook/whatsapp", json=doc_payload)
            alert_body = ""
            deadline = time.time() + 10.0
            while time.time() < deadline:
                if sent_bodies:
                    alert_body = sent_bodies[-1]
                    break
                time.sleep(0.2)
            text_payload = {
                "entry": [{
                    "changes": [{
                        "value": {
                            "messages": [{
                                "from": sender,
                                "id": "wamid-conf-2",
                                "timestamp": "1700000001",
                                "type": "text",
                                "text": {"body": "YES"},
                            }]
                        }
                    }]
                }]
            }
            sent_before = len(sent_bodies)
            resp2 = client.post("/webhook/whatsapp", json=text_payload)
            yes_body = ""
            deadline = time.time() + 10.0
            while time.time() < deadline:
                if len(sent_bodies) > sent_before:
                    yes_body = sent_bodies[-1]
                    break
                time.sleep(0.2)
            with get_conn() as c2:
                cur_row = c2.execute(
                    "SELECT fact FROM memories WHERE company_id = %s "
                    "AND is_latest AND fact LIKE 'Travel limit is 25000%%'",
                    (COMPANY,),
                ).fetchone()
            check(
                "f-webhook-alert-then-yes",
                resp.status_code == 200
                and alert_body.startswith("New doc says Travel limit is 25000")
                and alert_body.endswith("Replace? Reply YES/NO.")
                and resp2.status_code == 200
                and yes_body == conflicts.RESOLVED_YES_REPLY
                and cur_row is not None,
                f"alert={alert_body[:70]} yes={yes_body[:50]} row={cur_row}",
            )
        finally:
            wa_mod._download_media = real_download
            wa_mod._deliver = real_deliver
            llm_mod.extract_facts = real_extract
            os.environ.pop("WHATSAPP_TOKEN", None)
            os.environ.pop("WHATSAPP_PHONE_ID", None)
        cleanup(conn)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
