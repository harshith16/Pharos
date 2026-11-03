"""Targeted proofs for role-block with next step (P1-5).

Prerequisites: base schemas + schema_security.sql +
schema_access.sql applied, DATABASE_URL set.

Seeds (company "rb-co"):
- finance fact, visible_to cfo+ceo: quarterly budget,
- technical fact, visible_to cto+ceo: server migration deadline,
- general fact, all roles: office opens at 9 am.

Checks:
(a) detect_role_block: employee + finance -> (Finance, CFO, cfo);
    employee + technical -> (Technical, CTO, cto); cfo + finance
    -> None; employee + general -> None.
(b) _answer_text: employee finance question -> exactly
    "Restricted to Finance. Request access from CFO?" + pending row.
(c) YES with a CFO number configured (patched send) -> sent
    confirmation + row resolved_sent + send called with the number.
(d) YES with no CFO number -> plain not-configured reply +
    row resolved_unconfigured + no send.
(e) NO -> "Okay — no access request sent." + row resolved_no.
(f) simulated WhatsApp round-trip: block message, then YES ->
    sent confirmation.
(g) genuine NOT_FOUND is not role-blocked: exact not-found text.

Run: PYTHONPATH=. python scripts/test_roleblock.py
Exit code 0 iff every check passes.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import roleblock as rb_mod
from app.db import get_conn, scoped_conn
from app.store import write_fact

COMPANY = "rb-co"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))


def cleanup(conn):
    conn.execute(
        "DELETE FROM pending_access_requests WHERE company_id = %s",
        (COMPANY,),
    )
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


def pending_row(sender):
    with get_conn() as conn:
        return conn.execute(
            "SELECT scope_label, approver, status "
            "FROM pending_access_requests "
            "WHERE company_id = %s AND sender = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (COMPANY, sender),
        ).fetchone()


def main():
    # Cleanup on a plain connection first: its commit would clear
    # the scoped connection's SET LOCAL variables (RLS gotcha).
    # Each restricted write gets its own scoped connection: a
    # write_fact commit clears SET LOCAL, so sharing one scoped
    # connection across restricted writes breaks the second one.
    with get_conn() as conn:
        cleanup(conn)
    with scoped_conn("ceo", unlocked=True) as conn:
        write_fact(
            conn, COMPANY, "Quarterly budget is 500000 rupees.",
            visible_to=["cfo", "ceo"],
        )
    with scoped_conn("ceo", unlocked=True) as conn:
        write_fact(
            conn, COMPANY, "Server migration deadline is 15 December 2026.",
            visible_to=["cto", "ceo"],
        )
    with scoped_conn("ceo", unlocked=True) as conn:
        write_fact(
            conn, COMPANY, "Office opens at 9 am.",
            visible_to=["employee", "cfo", "cto", "ceo"],
        )

    # (a) Detection across roles and scopes.
    check(
        "a-detect-finance",
        rb_mod.detect_role_block(
            COMPANY, "employee", "What is the quarterly budget?"
        ) == ("Finance", "CFO", "cfo"),
        "employee finance blocked",
    )
    check(
        "a-detect-technical",
        rb_mod.detect_role_block(
            COMPANY, "employee",
            "What is the server migration deadline?",
        ) == ("Technical", "CTO", "cto"),
        "employee technical blocked",
    )
    check(
        "a-detect-cfo-sees",
        rb_mod.detect_role_block(
            COMPANY, "cfo", "What is the quarterly budget?"
        ) is None,
        "cfo sees finance",
    )
    check(
        "a-detect-general-open",
        rb_mod.detect_role_block(
            COMPANY, "employee", "When does the office open?"
        ) is None,
        "general open to employee",
    )

    import app.whatsapp as wa_mod
    from app import config as cfg_mod

    sender_b = "910000000501"
    # (b) _answer_text role-block message + pending row.
    reply_b = wa_mod._answer_text(
        COMPANY, "employee", "What is the quarterly budget?", sender_b
    )
    row_b = pending_row(sender_b)
    check(
        "b-block-message-pending",
        reply_b == "Restricted to Finance. Request access from CFO?"
        and row_b is not None
        and row_b[0] == "Finance"
        and row_b[1] == "CFO"
        and row_b[2] == "pending",
        f"reply={reply_b} row={row_b}",
    )

    # (c) YES with a CFO number -> sent confirmation.
    real_phone = getattr(cfg_mod, "WHATSAPP_CFO_PHONE", "")
    real_send = rb_mod.send_access_message
    sent_calls = []

    def _capture_send(phone, body):
        sent_calls.append((phone, body))
        return True, None

    rb_mod.send_access_message = _capture_send
    cfg_mod.WHATSAPP_CFO_PHONE = "919999999999"
    try:
        reply_c = wa_mod._answer_text(COMPANY, "employee", "YES", sender_b)
        row_c = pending_row(sender_b)
        check(
            "c-yes-sends",
            reply_c == "Done — I sent your access request to the CFO."
            and len(sent_calls) == 1
            and sent_calls[0][0] == "919999999999"
            and row_c is not None
            and row_c[2] == "resolved_sent",
            f"reply={reply_c} row={row_c}",
        )
    finally:
        rb_mod.send_access_message = real_send

    # (d) YES with no CFO number -> plain not-configured reply.
    sender_d = "910000000502"
    wa_mod._answer_text(
        COMPANY, "employee", "What is the quarterly budget?", sender_d
    )
    cfg_mod.WHATSAPP_CFO_PHONE = ""
    sent_calls.clear()
    rb_mod.send_access_message = _capture_send
    try:
        reply_d = wa_mod._answer_text(COMPANY, "employee", "YES", sender_d)
        row_d = pending_row(sender_d)
        check(
            "d-yes-unconfigured",
            reply_d == (
                "No CFO number is configured, so I couldn't send "
                "the access request."
            )
            and sent_calls == []
            and row_d is not None
            and row_d[2] == "resolved_unconfigured",
            f"reply={reply_d} row={row_d}",
        )
    finally:
        rb_mod.send_access_message = real_send
        cfg_mod.WHATSAPP_CFO_PHONE = real_phone

    # (e) NO declines.
    sender_e = "910000000503"
    wa_mod._answer_text(
        COMPANY, "employee", "What is the quarterly budget?", sender_e
    )
    reply_e = wa_mod._answer_text(COMPANY, "employee", "NO", sender_e)
    row_e = pending_row(sender_e)
    check(
        "e-no-declines",
        reply_e == "Okay — no access request sent."
        and row_e is not None
        and row_e[2] == "resolved_no",
        f"reply={reply_e} row={row_e}",
    )

    # (g) Genuine NOT_FOUND is not role-blocked.
    reply_g = wa_mod._answer_text(
        COMPANY, "employee",
        "What is the stock option vesting schedule?", "910000000504",
    )
    check(
        "g-genuine-not-found",
        reply_g == "Not found in company documents.",
        f"reply={reply_g}",
    )

    # (f) Simulated WhatsApp round-trip.
    real_deliver = wa_mod._deliver
    sent_bodies = []

    async def _fake_deliver(client, phone_id, token, sender_to, body):
        sent_bodies.append(body)
        return "sent", "fake-msg-id"

    wa_mod._deliver = _fake_deliver
    rb_mod.send_access_message = _capture_send
    cfg_mod.WHATSAPP_CFO_PHONE = "919999999999"
    try:
        from fastapi.testclient import TestClient

        from app.main import app

        os.environ["COMPANY_ID"] = COMPANY
        os.environ["WHATSAPP_TOKEN"] = "dummy-token"
        os.environ["WHATSAPP_PHONE_ID"] = "12345"
        os.environ["MEDIA_DIR"] = tempfile.mkdtemp(prefix="wa_rb_")
        client = TestClient(app)

        def _post_text(mid, body):
            payload = {
                "entry": [{
                    "changes": [{
                        "value": {
                            "messages": [{
                                "from": "910000000505",
                                "id": mid,
                                "timestamp": "1700000000",
                                "type": "text",
                                "text": {"body": body},
                            }]
                        }
                    }]
                }]
            }
            return client.post("/webhook/whatsapp", json=payload)

        def _wait_body():
            deadline = time.time() + 10.0
            while time.time() < deadline:
                if sent_bodies:
                    return sent_bodies[-1]
                time.sleep(0.2)
            return ""

        sent_calls.clear()
        sent_bodies.clear()
        resp_f1 = _post_text(
            "wamid-rb-1", "What is the quarterly budget?"
        )
        body_f1 = _wait_body()
        sent_bodies.clear()
        resp_f2 = _post_text("wamid-rb-2", "YES")
        body_f2 = _wait_body()
        check(
            "f-webhook-block-then-yes",
            resp_f1.status_code == 200
            and body_f1 == "Restricted to Finance. Request access from CFO?"
            and resp_f2.status_code == 200
            and body_f2 == "Done — I sent your access request to the CFO."
            and len(sent_calls) == 1,
            f"block={body_f1} yes={body_f2}",
        )
    finally:
        wa_mod._deliver = real_deliver
        rb_mod.send_access_message = real_send
        cfg_mod.WHATSAPP_CFO_PHONE = real_phone
        os.environ.pop("WHATSAPP_TOKEN", None)
        os.environ.pop("WHATSAPP_PHONE_ID", None)

    with get_conn() as conn:
        cleanup(conn)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
