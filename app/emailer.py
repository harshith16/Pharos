"""Email helpers for the connect-flow Gmail connector.

There is NO SMTP path anymore and NO auth in .env. Sending goes
only through app/gmail_api.py, using the token saved by the
/browser connect flow (/auth/gmail). This module only resolves
recipients and renders the facts-only email body.

Recipient resolution, in order:
1. Recipients saved on the connect page (data/gmail_connect.json),
   keyed by role name — entered once by the founder, never in .env.
2. The default recipient saved on the connect page, if any.
3. A recipient-only .env value per role, e.g. HR_EMAIL — an
   address, never a credential. A role with no address anywhere
   simply has no recipient; that is a configuration gap, never a
   reason to invent one.
"""

import logging
import os

_log = logging.getLogger("email_connector")


def resolve_recipient(target_display: str):
    """Recipient address for a target entity.

    Candidate keys are derived from the entity's own display name,
    never from a hand-typed role list: the full name uppercased,
    then the first token alone. Stored connect-page recipients
    are checked first, then the matching recipient-only .env
    value. Returns (address, source) where source is "connect"
    or the .env key used, or (None, None) when no candidate is
    configured. The address is returned, never logged here;
    callers audit it as part of the action record.
    """
    tokens = [t for t in (target_display or "").replace("_", " ").split() if t]
    if not tokens:
        return None, None
    candidates = [" ".join(t.upper() for t in tokens)]
    if len(tokens) > 1:
        candidates.append(tokens[0].upper())
    try:
        from app import gmail_api

        stored = gmail_api.load_recipients()
    except Exception:
        stored = {}
    for key in candidates:
        if stored.get(key):
            return stored[key], "connect"
    if stored.get("DEFAULT"):
        return stored["DEFAULT"], "connect"
    for key in candidates:
        env_key = key.replace(" ", "_") + "_EMAIL"
        value = (os.environ.get(env_key) or "").strip()
        if value:
            return value, env_key
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
