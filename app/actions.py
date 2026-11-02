"""Execute a router-decided ACT: the bot really emails someone.

This module executes; it never decides. The engine router has
already decided the route, the target (an entity from uploaded data),
the topic, and the covering facts. Here those are turned into a real
email, with honest failure modes at every step and an audit record
for every attempt.

Sending order (founder order): Gmail API via OAuth FIRST. SMTP is
only a fallback when OAuth is NOT configured — never a silent retry
after an OAuth attempt (that could double-send if Gmail accepted the
first attempt but the response was lost).

- neither OAuth nor SMTP configured -> the WhatsApp reply says so
  plainly, and nothing is sent. Never a fake confirmation.
- no recipient address for the target -> the reply says so plainly.
- send failure on the attempted path -> the reply says the email
  could not be sent, the real error is logged, no success claimed.
- success -> the reply confirms what was sent and to which role.

The email body comes from engine facts only: the LLM composes from
the covering facts when a key is available, otherwise the body is
the deterministic facts rendering. Either way no content can appear
that is not in the facts.

Audit: every attempt writes a row to email_actions when the table
exists, and always writes a structured log line, so an attempt is
never invisible even before the migration is applied on a machine.
"""

import logging

from app import emailer

_log = logging.getLogger("email_actions")

EMAIL_NOT_CONFIGURED_REPLY = (
    "I can't send email yet — email isn't configured. "
    "Ask whoever runs this bot to set the Gmail OAuth or SMTP details in .env."
)


def _audit(conn, company_id, sender, sender_role, target, topic,
           recipient, status, error=""):
    """Best-effort audit row; always a structured log line.

    A missing table (migration not yet applied on this machine) must
    never break the action — the log line is the audit then.
    """
    _log.info(
        "email_action company=%s sender=%s role=%s target=%s topic=%s "
        "recipient=%s status=%s error=%s",
        company_id, sender, sender_role, target, topic,
        recipient or "-", status, error or "-",
    )
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO email_actions "
                "(company_id, sender, sender_role, target_role, topic, "
                " recipient, status, error) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (company_id, sender or "", sender_role or "employee",
                 target or "", topic or "", recipient or "", status,
                 error or ""),
            )
    except Exception:
        # Table missing or any write problem: the log line above is
        # the audit. Never let auditing break the action itself.
        _log.warning("email_actions audit row could not be written")


def handle_act(conn, company_id, role, sender, route_result) -> str:
    """Send the router-decided email; return the WhatsApp reply text.

    route_result carries route=ACT, target_display, topic, and the
    role-filtered covering facts. Returns only honest replies: a
    confirmation naming the target and topic on success, or a plain
    failure/config message otherwise. Never raises.
    """
    try:
        target = (route_result or {}).get("target_display") or ""
        topic = (route_result or {}).get("topic") or "your request"
        facts = (route_result or {}).get("facts") or []
        if not target or not facts:
            _audit(conn, company_id, sender, role, target, topic,
                   "", "failed", "ACT result missing target or facts")
            return "I couldn't send that — I don't have the details needed."
        # Transport check first: when neither Gmail OAuth nor SMTP is
        # configured, the honest reply is "not configured" — even if
        # a recipient address is also missing. Recipient resolution
        # only matters once some transport exists.
        try:
            from app import gmail_api

            oauth_config = gmail_api.get_oauth_config()
            oauth_ready = gmail_api.is_oauth_ready(oauth_config)
        except Exception:
            oauth_config, oauth_ready = None, False
        smtp_settings = emailer.get_smtp_settings()
        if not oauth_ready and smtp_settings is None:
            _audit(conn, company_id, sender, role, target, topic,
                   "", "not_configured")
            return EMAIL_NOT_CONFIGURED_REPLY
        recipient, _key = emailer.resolve_recipient(target)
        if not recipient:
            _audit(conn, company_id, sender, role, target, topic,
                   "", "no_recipient")
            return (
                f"I don't have an email address configured for {target}, "
                "so I couldn't send that email."
            )
        body = ""
        try:
            from app import llm

            body = llm.compose_email(target, topic, facts) or ""
        except Exception:
            body = ""
        if not body.strip():
            subject, body = emailer.render_facts_email(target, topic, facts)
        else:
            subject = f"Request regarding {topic}"
        # Primary path: Gmail API via OAuth. SMTP is only the
        # fallback when OAuth is not configured — never a silent
        # retry after an OAuth attempt (see module docstring).
        if oauth_ready:
            ok, error = gmail_api.send_via_gmail(
                recipient, subject, body, oauth_config=oauth_config
            )
            if ok:
                _audit(conn, company_id, sender, role, target, topic,
                       recipient, "sent_gmail")
                return f"Done — I emailed {target} about {topic}."
            _log.error("gmail send failed target=%s error=%s", target, error)
            _audit(conn, company_id, sender, role, target, topic,
                   recipient, "failed", error or "")
            return (
                f"I couldn't send that email to {target}. "
                "Please try again later."
            )
        # Fallback only: OAuth not configured, so SMTP is the path.
        # smtp_settings was verified non-None by the transport check
        # above; re-reading here is just belt and braces.
        settings = smtp_settings or emailer.get_smtp_settings()
        if settings is None:
            _audit(conn, company_id, sender, role, target, topic,
                   "", "not_configured")
            return EMAIL_NOT_CONFIGURED_REPLY
        ok, error = emailer.send_email(recipient, subject, body,
                                       settings=settings)
        if ok:
            _audit(conn, company_id, sender, role, target, topic,
                   recipient, "sent_smtp")
            return f"Done — I emailed {target} about {topic}."
        _log.error("smtp send failed target=%s error=%s", target, error)
        _audit(conn, company_id, sender, role, target, topic,
               recipient, "failed", error or "")
        return (
            f"I couldn't send that email to {target}. "
            "Please try again later."
        )
    except Exception as exc:  # noqa: BLE001 - honest reply, never crash
        _log.exception("handle_act failed: %s", exc)
        return "I couldn't send that email. Please try again later."
