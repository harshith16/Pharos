"""8-digit code gate for sensitive information (second factor).

Layering: role gating still applies first and always. A sensitive
fact is one whose stored visible_to lacks 'employee' (finance,
technical, and any other restricted scope) — the flag comes from
engine data via the is_sensitive column, never from a word list.
When a question's covering facts include sensitive rows, the bot
asks for the 8-digit code instead of answering, even for an
otherwise authorised role. After unlock, the pending question is
answered under the normal role filter, so an employee with the code
still gets nothing role-hidden.

Storage: only a PBKDF2-HMAC-SHA256 hash with a random salt ever
reaches the database (see scripts/set_auth_code.py). Verification
uses hmac.compare_digest (constant time). The plaintext code is
never logged, never returned, and never appears in code or tests.

State is per sender, DB-backed, in schema_security.sql tables:
- pending_questions: the question held while awaiting the code.
- code_unlocks: unlocked_until (30 minutes from a correct code).
- code_attempts: consecutive failures + locked_until (15 minutes
  after 3 consecutive failures).

Pure decisions (no I/O) live here so tests can call them directly;
the DB helpers take an open connection and never commit (the
caller's context manager owns the transaction).
"""

import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta, timezone

_log = logging.getLogger("code_gate")

UNLOCK_MINUTES = 30
MAX_FAILURES = 3
LOCKOUT_MINUTES = 15

CODE_RE = re.compile(r"^\d{8}$")

CODE_CHALLENGE_REPLY = (
    "That question covers restricted information. "
    "Please reply with the 8-digit authentication code to continue."
)
CODE_INCORRECT_REPLY = "That code is incorrect. Please try again."
CODE_LOCKED_REPLY = (
    "Too many incorrect attempts. Code entry is locked for 15 minutes. "
    "Please try again later."
)
# Note: an expired unlock window surfaces as a fresh code challenge
# on the next sensitive question, not a separate expiry reply.


def is_code_attempt(text: str) -> bool:
    """True only for a message of exactly 8 digits. Anything else is
    an ordinary message, never a code attempt."""
    return bool(text) and CODE_RE.match(text.strip()) is not None


def hash_code(code: str, salt_hex: str) -> str:
    """PBKDF2-HMAC-SHA256 (200k iterations); returns the hex digest.
    The salt is stored alongside the hash; the code itself is not."""
    salt = bytes.fromhex(salt_hex)
    return hashlib.pbkdf2_hmac(
        "sha256", code.encode("utf-8"), salt, 200_000
    ).hex()


def verify_code(code: str, salt_hex: str, expected_hash: str) -> bool:
    """Constant-time comparison of the derived hash."""
    try:
        derived = hash_code(code, salt_hex)
    except Exception:
        return False
    return hmac.compare_digest(derived, expected_hash or "")


# --- DB state helpers (caller owns commit) -------------------------

def get_stored_code(conn):
    """Most recent (salt_hex, code_hash), or None when no code was
    ever set up. No code configured means the gate cannot open."""
    row = conn.execute(
        "SELECT salt, code_hash FROM auth_codes "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return row[0], row[1]


def is_locked(conn, sender: str, now=None) -> bool:
    """True while a lockout window runs for this sender."""
    now = now or datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT locked_until FROM code_attempts WHERE sender = %s",
        (sender,),
    ).fetchone()
    return bool(row and row[0] is not None and row[0] > now)


def is_unlocked(conn, sender: str, now=None) -> bool:
    """True while the sender's unlock window runs."""
    now = now or datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT unlocked_until FROM code_unlocks WHERE sender = %s",
        (sender,),
    ).fetchone()
    return bool(row and row[0] is not None and row[0] > now)


def record_failure(conn, sender: str, now=None):
    """One more consecutive failure; on the third, lock 15 minutes.
    Returns True when this failure triggered the lockout."""
    now = now or datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT failures FROM code_attempts WHERE sender = %s",
        (sender,),
    ).fetchone()
    failures = (row[0] if row else 0) + 1
    locked_until = None
    if failures >= MAX_FAILURES:
        locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
    conn.execute(
        "INSERT INTO code_attempts (sender, failures, locked_until, "
        "updated_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (sender) DO UPDATE SET failures = EXCLUDED.failures, "
        "locked_until = EXCLUDED.locked_until, updated_at = EXCLUDED.updated_at",
        (sender, failures, locked_until, now),
    )
    _log.info(
        "code attempt failed sender=%s failures=%s locked=%s",
        sender,
        failures,
        locked_until is not None,
    )
    return locked_until is not None


def record_success(conn, sender: str, now=None):
    """Clear failures and open a 30-minute unlock window."""
    now = now or datetime.now(timezone.utc)
    until = now + timedelta(minutes=UNLOCK_MINUTES)
    conn.execute(
        "INSERT INTO code_unlocks (sender, unlocked_until, updated_at) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (sender) DO UPDATE SET unlocked_until = "
        "EXCLUDED.unlocked_until, updated_at = EXCLUDED.updated_at",
        (sender, until, now),
    )
    conn.execute("DELETE FROM code_attempts WHERE sender = %s", (sender,))
    _log.info("code accepted sender=%s unlocked_until=%s", sender, until)


def store_pending(conn, sender: str, company_id: str, role: str, question: str):
    """Hold the question while the sender is asked for the code. A
    new challenge replaces any previous pending question."""
    conn.execute(
        "INSERT INTO pending_questions (sender, company_id, role, question) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (sender) DO UPDATE SET company_id = EXCLUDED.company_id, "
        "role = EXCLUDED.role, question = EXCLUDED.question, "
        "created_at = now()",
        (sender, company_id, role, question),
    )


def peek_pending(conn, sender: str):
    """Return (company_id, role, question) without consuming, or None."""
    row = conn.execute(
        "SELECT company_id, role, question FROM pending_questions "
        "WHERE sender = %s",
        (sender,),
    ).fetchone()
    if row is None:
        return None
    return row[0], row[1], row[2]


def take_pending(conn, sender: str):
    """Pop and return (company_id, role, question), or None."""
    row = peek_pending(conn, sender)
    if row is None:
        return None
    conn.execute("DELETE FROM pending_questions WHERE sender = %s", (sender,))
    return row


def has_pending(conn, sender: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pending_questions WHERE sender = %s", (sender,)
    ).fetchone()
    return row is not None


def find_sensitive_covering(conn, company_id: str, role: str, question: str):
    """Covering facts for the question, run at CEO scope with the
    unlock ON, so this probe can see what the role-filtered answer
    cannot. Returns (sensitive_facts, role_visible_facts): facts the
    sender's role may see (for the post-unlock answer check) and
    the sensitive subset among them.

    This probe runs server-side only; its rows are never returned
    to the sender. It answers one question: does this question
    touch sensitive information the sender's role is allowed to see
    (code challenge), or is it role-hidden entirely (not a code
    case — the role filter, not the code, decides)?
    """
    from app import retrieve
    from app.db import scoped_conn
    from app.engine import query as engine_query

    try:
        alias_map = {}
        try:
            from app.engine.router import load_alias_map

            alias_map = load_alias_map(conn, company_id)
        except Exception:
            alias_map = {}
        with scoped_conn("ceo", unlocked=True) as probe_conn:
            found = retrieve.search(
                probe_conn, company_id, "ceo", question, alias_map=alias_map
            )
            covering = retrieve.filter_covering(
                found, question, alias_map=alias_map
            )
            sensitive = []
            role_visible = []
            for fact in covering or []:
                vis = fact.get("visible_to") or []
                if _role_may_see(role, vis):
                    role_visible.append(fact)
                    if "employee" not in vis:
                        sensitive.append(fact)
            # Graph-only coverage: a question answered from a path
            # has no legacy facts, so check the traversed relations
            # themselves. Path finding only (no synthesis) keeps
            # this probe free of model calls.
            try:
                names = engine_query._link_entities(
                    probe_conn, company_id, question
                )
                path = []
                if len(names) >= 2:
                    found_path = engine_query._find_path(
                        probe_conn, company_id, "ceo", names[0], names[-1]
                    )
                    path = found_path["path"] if found_path else []
                elif len(names) == 1:
                    paths = engine_query._traverse(
                        probe_conn, company_id, "ceo", names[0]
                    )
                    path = paths[0]["path"] if paths else []
                for step in path:
                    if not isinstance(step, dict):
                        continue
                    frm = step.get("from")
                    rel = step.get("relationship")
                    to = step.get("to")
                    if not (frm and rel and to):
                        continue
                    # A reversed step traverses against the stored
                    # direction, so stored src/dst swap.
                    if step.get("reversed"):
                        src_name, dst_name = to, frm
                    else:
                        src_name, dst_name = frm, to
                    row = probe_conn.execute(
                        "SELECT r.is_sensitive, r.visible_to "
                        "FROM engine_relations r "
                        "JOIN engine_entities s ON s.id = r.src_id "
                        "JOIN engine_entities d ON d.id = r.dst_id "
                        "WHERE r.company_id = %s AND s.display_name = %s "
                        "AND d.display_name = %s AND r.rel = %s",
                        (company_id, src_name, dst_name, rel),
                    ).fetchone()
                    if row and row[0] and _role_may_see(role, row[1]):
                        sensitive.append(
                            {
                                "fact": f"{frm} {rel} {to}",
                                "visible_to": list(row[1] or []),
                            }
                        )
            except Exception:
                pass
            return sensitive, role_visible
    except Exception:
        return [], []


def _role_may_see(role: str, visible_to) -> bool:
    """Mirror of the app role predicates, over a fact's visible_to."""
    vis = set(visible_to or [])
    if role == "ceo":
        return True
    if role == "cfo":
        return bool(vis & {"cfo", "employee"})
    if role == "cto":
        return bool(vis & {"cto", "employee"})
    if role == "manager":
        return bool(vis & {"manager", "employee"})
    return "employee" in vis
