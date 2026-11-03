"""Role-block with next step (P1-5).

Detection is data-driven, never a word list: a question is
role-blocked when CEO-scope covering facts exist for it but none
of them is visible to the sender's role (the same role predicate
the app already enforces). The scope label and approver come from
the hidden facts' stored visible_to: finance -> Finance/CFO,
technical -> Technical/CTO, anything else -> Executive/CEO.

Flow: _answer_text returns "Restricted to <Scope>. Request
access from <Approver>?" and stores one pending request per
sender+company (a new block replaces the previous one). A later
YES sends a WhatsApp message to the approver's number from the
existing role-phone config and logs it; with no number configured
the reply says so plainly and logs that; a transport failure
keeps the request pending so YES can be retried. Nothing is ever
faked: a failed send is reported as a failure, never a success.
"""

import logging

from app.db import get_conn, scoped_conn

_log = logging.getLogger(__name__)


def scope_for(visible_to):
    """(scope label, approver, approver role) for a hidden fact."""
    vis = visible_to or []
    if "cfo" in vis:
        return ("Finance", "CFO", "cfo")
    if "cto" in vis:
        return ("Technical", "CTO", "cto")
    return ("Executive", "CEO", "ceo")


def detect_role_block(company_id: str, role: str, question: str):
    """(scope_label, approver, approver_role) when the question is
    covered only by facts the sender's role may not see; else None.
    Probes at CEO scope (like the code gate's probe) and
    defensively requires sender-scope coverage to be empty.
    Never raises."""
    try:
        from app import retrieve
        from app.engine.router import load_alias_map
        from app.security import _role_may_see

        with get_conn() as conn:
            try:
                alias_map = load_alias_map(conn, company_id)
            except Exception:
                alias_map = {}
        with scoped_conn("ceo", unlocked=True) as probe:
            found = retrieve.search(
                probe, company_id, "ceo", question, alias_map=alias_map
            )
            covering = retrieve.filter_covering(
                found, question, alias_map=alias_map
            )
        hidden = [
            fact for fact in (covering or [])
            if not _role_may_see(role, fact.get("visible_to") or [])
        ]
        if not hidden:
            return None
        with scoped_conn(role, unlocked=False) as sconn:
            s_found = retrieve.search(
                sconn, company_id, role, question, alias_map=alias_map
            )
            s_covering = retrieve.filter_covering(
                s_found, question, alias_map=alias_map
            )
        if s_covering:
            return None
        return scope_for(hidden[0].get("visible_to") or [])
    except Exception:
        _log.exception("detect_role_block failed; treating as not blocked")
        return None


def store_request(company_id: str, sender: str, scope_label: str,
                  approver: str, approver_role: str,
                  question: str) -> None:
    """One pending request per sender+company; a new block
    replaces the previous pending one."""
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE pending_access_requests SET status = 'resolved_no' "
                "WHERE company_id = %s AND sender = %s AND status = 'pending'",
                (company_id, sender),
            )
            conn.execute(
                "INSERT INTO pending_access_requests "
                "(company_id, sender, scope_label, approver, "
                " approver_role, question, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'pending')",
                (company_id, sender, scope_label, approver,
                 approver_role, question or ""),
            )
            conn.commit()
    except Exception:
        _log.exception("store_request failed")


def peek_request(company_id: str, sender: str):
    """The sender's pending request row, or None."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT id, scope_label, approver, approver_role, "
                "question FROM pending_access_requests "
                "WHERE company_id = %s AND sender = %s "
                "AND status = 'pending' "
                "ORDER BY created_at DESC LIMIT 1",
                (company_id, sender),
            ).fetchone()
            return row
    except Exception:
        _log.exception("peek_request failed")
        return None


def _set_status(request_id, status: str) -> None:
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE pending_access_requests SET status = %s "
                "WHERE id = %s",
                (status, request_id),
            )
            conn.commit()
    except Exception:
        _log.exception("_set_status failed")


def approver_phone(approver_role: str):
    """First configured number for the approver role, or None.
    CEO falls back to the legacy manager number, mirroring _role_for."""
    try:
        from app import config as cfg
        from app.whatsapp import _parse_phones

        if approver_role == "cfo":
            value = cfg.WHATSAPP_CFO_PHONE
        elif approver_role == "cto":
            value = cfg.WHATSAPP_CTO_PHONE
        else:
            value = cfg.WHATSAPP_CEO_PHONE or cfg.WHATSAPP_MANAGER_PHONE
        numbers = _parse_phones(value)
        # _parse_phones returns a set: sort for a deterministic pick.
        return sorted(numbers)[0] if numbers else None
    except Exception:
        _log.exception("approver_phone failed")
        return None


def send_access_message(phone: str, body: str):
    """Sync Graph send to one number. Returns (ok, detail); a
    missing token/phone id is (False, 'not_configured'), never a
    silent success."""
    try:
        from app import config as cfg
        from app.whatsapp import GRAPH_BASE, build_send_payload

        token = cfg.WHATSAPP_TOKEN or cfg.WHATSAPP_ACCESS_TOKEN
        phone_id = cfg.WHATSAPP_PHONE_ID or cfg.WHATSAPP_PHONE_NUMBER_ID
        if not token or not phone_id or not phone:
            return False, "not_configured"
        import httpx

        with httpx.Client(timeout=30.0, trust_env=False) as client:
            resp = client.post(
                f"{GRAPH_BASE}/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json=build_send_payload(phone, body),
            )
        if resp.status_code >= 400:
            return False, f"graph_{resp.status_code}"
        return True, None
    except Exception as exc:
        return False, str(exc)


def resolve_access_request(company_id: str, sender: str, yes: bool):
    """Resolve the sender's pending access request. Returns the
    reply text, or None when nothing is pending (caller routes
    normally). Never raises."""
    try:
        req = peek_request(company_id, sender)
        if req is None:
            return None
        request_id, scope_label, approver, approver_role, question = req
        if not yes:
            _set_status(request_id, "resolved_no")
            _log.info(
                "access request declined company=%s sender=%s "
                "approver=%s", company_id, sender, approver
            )
            return "Okay — no access request sent."
        phone = approver_phone(approver_role)
        if not phone:
            _set_status(request_id, "resolved_unconfigured")
            _log.info(
                "access request unconfigured company=%s sender=%s "
                "approver=%s", company_id, sender, approver
            )
            return (
                f"No {approver} number is configured, so I couldn't "
                "send the access request."
            )
        body = (
            f"Access request: {sender} asked about '{question}' and "
            f"was told it is restricted to {scope_label}. They asked "
            "to request access from you. Please reply to them directly."
        )
        ok, detail = send_access_message(phone, body)
        if ok:
            _set_status(request_id, "resolved_sent")
            _log.info(
                "access request sent company=%s sender=%s approver=%s "
                "phone_set=yes", company_id, sender, approver
            )
            return f"Done — I sent your access request to the {approver}."
        _log.info(
            "access request send failed company=%s sender=%s "
            "approver=%s detail=%s", company_id, sender, approver, detail
        )
        return (
            f"I couldn't send the access request to the {approver}. "
            "Please try again later."
        )
    except Exception:
        _log.exception("resolve_access_request failed")
        return "I couldn't send that access request. Please try again later."
