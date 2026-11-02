"""Real email connector: standard-library SMTP only, no stubs.

The bot sends real mail through a real SMTP server. There is no
"pretend sent" path anywhere in this module: send_email() performs
an actual SMTP conversation (EHLO, STARTTLS when requested, LOGIN,
MAIL FROM, RCPT TO, DATA, QUIT) and reports the real outcome. A
failure returns (False, error) — callers must report it honestly,
never claim a success that did not happen.

Configuration comes from .env ONLY (read live, never printed):
- SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587)
- SMTP_USER, SMTP_APP_PASSWORD (for Gmail: an App Password, not the
  account password)
- <ROLE>_EMAIL per role, e.g. HR_EMAIL — the recipient address for
  that role. A role with no configured address simply has no
  recipient; that is a configuration gap, never a reason to invent one.
"""

import logging
import os
import smtplib
from email.message import EmailMessage

from app import config

_log = logging.getLogger("email_connector")

DEFAULT_HOST = "smtp.gmail.com"
DEFAULT_PORT = 587


def get_smtp_settings() -> dict | None:
    """SMTP settings from .env, or None when credentials are missing.

    Returns None (never partial credentials) when SMTP_USER or
    SMTP_APP_PASSWORD is empty after stripping. Host/port fall back
    to the Gmail defaults. Values are never logged.
    """
    user = (config.get("SMTP_USER") or "").strip()
    password = (config.get("SMTP_APP_PASSWORD") or "").strip()
    if not user or not password:
        return None
    host = (config.get("SMTP_HOST") or "").strip() or DEFAULT_HOST
    try:
        port = int((config.get("SMTP_PORT") or "").strip() or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    return {"host": host, "port": port, "user": user, "password": password}


def resolve_recipient(target_display: str):
    """Recipient address for a target entity, from .env only.

    Candidate keys are derived from the entity's own display name,
    never from a hand-typed role list: the full name uppercased with
    underscores (HR Lead -> HR_LEAD_EMAIL), then the first token
    (HR Lead -> HR_EMAIL). Returns (address, key_used) or (None, None)
    when no candidate key is configured. The address is returned, never
    logged here; callers audit it as part of the action record.
    """
    tokens = [t for t in (target_display or "").replace("_", " ").split() if t]
    if not tokens:
        return None, None
    candidates = []
    full = "_".join(t.upper() for t in tokens) + "_EMAIL"
    candidates.append(full)
    if len(tokens) > 1:
        candidates.append(tokens[0].upper() + "_EMAIL")
    for key in candidates:
        value = (os.environ.get(key) or "").strip()
        if value:
            return value, key
    return None, None


def render_facts_email(target_display: str, topic: str, facts: list):
    """Deterministic email from engine facts only — no LLM needed.

    Subject names the topic; the body lists the covering facts
    verbatim and nothing else. No figure, name, or claim can appear
    here unless it is in the supplied facts, so this path can never
    invent content. Used whenever the LLM is unavailable (no key) and
    as the fallback when LLM composition returns nothing.
    """
    topic_text = (topic or "your request").strip() or "your request"
    subject = f"Request regarding {topic_text}"
    lines = [
        f"Hello {target_display},",
        "",
        f"An employee has asked to inform you about {topic_text}.",
        "The relevant company policy details on file are:",
        "",
    ]
    for fact in facts or []:
        text = fact.get("fact") if isinstance(fact, dict) else None
        if isinstance(text, str) and text.strip():
            lines.append(f"- {text.strip()}")
    lines += [
        "",
        "Sent via the company assistant on behalf of the employee.",
    ]
    return subject, "\n".join(lines)


def send_email(to: str, subject: str, body: str, settings: dict | None = None,
               smtp_class=smtplib.SMTP, use_tls: bool = True):
    """Send one real email. Returns (True, None) or (False, error).

    Never raises: every failure (DNS, connection, auth, send) is
    returned as (False, error string) so the caller can report it
    honestly. smtp_class is injectable for tests; the default is the
    real smtplib client, and use_tls=True performs a real STARTTLS
    upgrade (the Gmail path). use_tls=False exists only for local
    test SMTP servers that do not offer TLS — the SMTP conversation
    itself is still real.
    """
    if settings is None:
        settings = get_smtp_settings()
    if settings is None:
        return False, "SMTP credentials are not configured"
    if not to or not to.strip():
        return False, "no recipient address"
    message = EmailMessage()
    message["From"] = settings["user"]
    message["To"] = to.strip()
    message["Subject"] = subject
    message.set_content(body or "")
    server = None
    try:
        server = smtp_class(settings["host"], settings["port"], timeout=30)
        server.ehlo()
        if use_tls:
            server.starttls()
            server.ehlo()
        server.login(settings["user"], settings["password"])
        server.sendmail(settings["user"], [to.strip()], message.as_string())
        return True, None
    except Exception as exc:  # noqa: BLE001 - error text is the report
        return False, str(exc)
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
