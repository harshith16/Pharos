"""Targeted proofs for Postgres RLS + the 8-digit code gate (main).

Prerequisites (same as every suite here): base schemas plus
schema_security.sql and schema_actions.sql applied, DATABASE_URL set.

The test code value is generated at RUNTIME (random 8 digits) and
exists only in process memory: no code literal appears in this file,
in logs, or in output. The wrong code is a different runtime-random
8-digit value. Checks never print either value.

Seeds (company "security-co"):
- a general travel fact (visible to all),
- a finance fact, visible_to cfo+ceo only, with a legacy triple,
  synced into an engine relation on a CEO-unlocked scoped
  connection (so the relation inherits the finance scope).

Checks:
(a) sensitive question without code -> code-challenge reply, zero
    facts leaked (no amount, no topic word, no entity name).
(b) wrong code -> incorrect reply, no facts, failure logged.
(c) correct code -> pending question answered at once under the
    role filter (graph path rendered), pending consumed, unlock set.
(d) 3 failed attempts -> temporary lockout (3rd wrong locks, a
    further code attempt still gets the locked reply).
(e) code expiry: a faked-expired unlock window challenges again.
(f) direct SQL proof of RLS: the same SELECT returns different
    rows under different session variables (hidden without unlock,
    visible with unlock + allowed role, still hidden for employee
    even with unlock — the role filter holds at the DB level).
(g) ACT email still flows through the WhatsApp answer path for a
    general topic: patched Gmail send captures the recipient from
    the connect-page stored default, audit row written. (The full
    local-server Gmail proof lives in test_act.py, which the gate
    also runs; this check proves the code gate does not break ACT.)
(h) grep proofs: no quoted 8-digit literal in app/, the setup
    script, or this file; no non-example email-address literal in
    app/ or scripts/ (reserved example.* test addresses allowed);
    no .env reads of Gmail/SMTP auth names in app/.

Run: PYTHONPATH=. python scripts/test_security.py
Exit code 0 iff every check passes. Cleans its own company rows first.
"""

import os
import re
import secrets
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import security
from app.db import get_conn, scoped_conn
from app.engine import relations
from app.store import write_fact
from app.whatsapp import _answer_text

COMPANY = "security-co"
RESULTS = []
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FINANCE_FACT = "The Finance Board approves the quarterly budget of 50000."
GENERAL_FACT = "Travel limit is 12000 rupees per trip."


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))


def _rand_code():
    return f"{secrets.randbelow(90000000) + 10000000:08d}"


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
    try:
        conn.execute("DELETE FROM email_actions WHERE company_id = %s", (COMPANY,))
    except Exception:
        conn.rollback()
    conn.execute("DELETE FROM pending_questions WHERE company_id = %s", (COMPANY,))
    conn.commit()


def grep_no_match(pattern, paths):
    """True when grep finds no match for pattern under paths."""
    proc = subprocess.run(
        ["grep", "-rnE", "--exclude-dir=__pycache__", pattern] + paths,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode != 0, proc.stdout.strip()[:200]


def main():
    correct = _rand_code()
    wrong = _rand_code()
    while wrong == correct:
        wrong = _rand_code()
    sender_a = "910000000001"
    sender_b = "910000000002"
    sender_g = "910000000003"

    with get_conn() as conn:
        cleanup(conn)
        for sender in (sender_a, sender_b, sender_g):
            conn.execute("DELETE FROM code_unlocks WHERE sender = %s", (sender,))
            conn.execute("DELETE FROM code_attempts WHERE sender = %s", (sender,))
            conn.execute(
                "DELETE FROM pending_questions WHERE sender = %s", (sender,)
            )
        conn.commit()

        # Seed: general (unscoped write works) + finance facts
        # (restricted-scope writes need operator scope, since
        # INSERT ... RETURNING is checked against the SELECT policy).
        write_fact(conn, COMPANY, GENERAL_FACT,
                   visible_to=["employee", "cfo", "cto", "ceo"],
                   source="document")
        with scoped_conn("ceo", unlocked=True) as wconn:
            write_fact(wconn, COMPANY, FINANCE_FACT,
                       visible_to=["cfo", "ceo"], source="document",
                       triples=[{"source": "finance_board",
                                 "relationship": "approves",
                                 "destination": "quarterly_budget"}])
        conn.commit()
        # Sync the finance triple on a CEO-unlocked connection so
        # the relation inherits the finance scope.
        with scoped_conn("ceo", unlocked=True) as sc:
            relations.sync_from_edges(sc, COMPANY)
        conn.commit()

        # Stored code hash (runtime value only, never printed).
        salt_hex = secrets.token_hex(16)
        digest = security.hash_code(correct, salt_hex)
        conn.execute("DELETE FROM auth_codes")
        conn.execute(
            "INSERT INTO auth_codes (salt, code_hash) VALUES (%s, %s)",
            (salt_hex, digest),
        )
        conn.commit()

        # Graph question built from the stored entity display names,
        # so entity linking is guaranteed regardless of casing.
        # The relation read needs operator scope: on the plain
        # connection RLS hides this restricted relation.
        with scoped_conn("ceo", unlocked=True) as sc:
            names = sc.execute(
                "SELECT s.display_name, d.display_name FROM engine_relations r "
                "JOIN engine_entities s ON s.id = r.src_id "
                "JOIN engine_entities d ON d.id = r.dst_id "
                "WHERE r.company_id = %s AND r.rel = 'approves'",
                (COMPANY,),
            ).fetchall()
        check("seed-finance-relation", bool(names), f"names={names}")
        if not names:
            print(f"\n{len(RESULTS) - 1}/{len(RESULTS)} checks passed")
            return 1
        src_display, dst_display = names[0]
        question = f"How is {src_display} connected to {dst_display}?"

        # (a) Sensitive question without code -> challenge, no leak.
        reply_a = _answer_text(COMPANY, "cfo", question, sender_a)
        leak_words = ("50000", "budget", "finance board", "quarterly")
        check(
            "a-challenge-no-leak",
            reply_a == security.CODE_CHALLENGE_REPLY
            and not any(w in reply_a.lower() for w in leak_words),
            f"reply={reply_a[:80]}",
        )
        check(
            "a-pending-stored",
            security.has_pending(conn, sender_a),
            "pending question held",
        )

        # (b) Wrong code -> incorrect, no facts, failure logged.
        reply_b = _answer_text(COMPANY, "cfo", wrong, sender_a)
        row_b = conn.execute(
            "SELECT failures FROM code_attempts WHERE sender = %s",
            (sender_a,),
        ).fetchone()
        check(
            "b-wrong-code-incorrect",
            reply_b == security.CODE_INCORRECT_REPLY
            and not any(w in reply_b.lower() for w in leak_words)
            and row_b is not None
            and row_b[0] == 1
            and security.has_pending(conn, sender_a),
            f"reply={reply_b[:60]} failures={row_b}",
        )

        # (c) Correct code -> pending answered under the role filter.
        reply_c = _answer_text(COMPANY, "cfo", correct, sender_a)
        check(
            "c-correct-code-answers",
            src_display in reply_c
            and dst_display in reply_c
            and not security.has_pending(conn, sender_a)
            and security.is_unlocked(conn, sender_a),
            f"reply={reply_c[:120]}",
        )

        # (d) Three failures -> lockout.
        _answer_text(COMPANY, "cfo", question, sender_b)
        r1 = _answer_text(COMPANY, "cfo", wrong, sender_b)
        r2 = _answer_text(COMPANY, "cfo", wrong, sender_b)
        r3 = _answer_text(COMPANY, "cfo", wrong, sender_b)
        r4 = _answer_text(COMPANY, "cfo", correct, sender_b)
        row_d = conn.execute(
            "SELECT failures, locked_until FROM code_attempts "
            "WHERE sender = %s",
            (sender_b,),
        ).fetchone()
        check(
            "d-three-failures-lockout",
            r1 == security.CODE_INCORRECT_REPLY
            and r2 == security.CODE_INCORRECT_REPLY
            and r3 == security.CODE_LOCKED_REPLY
            and r4 == security.CODE_LOCKED_REPLY
            and row_d is not None
            and row_d[0] == 3
            and row_d[1] is not None,
            f"r1={r1[:30]} r3={r3[:30]} r4={r4[:30]} row={row_d}",
        )

        # (e) Expired unlock window -> challenge again.
        conn.execute(
            "UPDATE code_unlocks SET unlocked_until = now() - interval "
            "'1 hour' WHERE sender = %s",
            (sender_a,),
        )
        conn.commit()
        reply_e = _answer_text(COMPANY, "cfo", question, sender_a)
        check(
            "e-expired-unlock-challenges",
            reply_e == security.CODE_CHALLENGE_REPLY
            and not security.is_unlocked(conn, sender_a),
            f"reply={reply_e[:60]}",
        )

        # (f) Direct SQL proof of RLS: same query, different vars.
        def _finance_rows(ctx):
            with ctx as c:
                return c.execute(
                    "SELECT fact FROM memories WHERE company_id = %s "
                    "AND fact LIKE '%%quarterly budget%%'",
                    (COMPANY,),
                ).fetchall()

        hidden_default = _finance_rows(get_conn())
        hidden_cfo_locked = _finance_rows(scoped_conn("cfo", unlocked=False))
        visible_cfo_unlocked = _finance_rows(scoped_conn("cfo", unlocked=True))
        hidden_emp_unlocked = _finance_rows(
            scoped_conn("employee", unlocked=True)
        )
        rel_hidden = None
        rel_visible = None
        with scoped_conn("cfo", unlocked=False) as c:
            rel_hidden = c.execute(
                "SELECT rel FROM engine_relations WHERE company_id = %s "
                "AND rel = 'approves'",
                (COMPANY,),
            ).fetchall()
        with scoped_conn("cfo", unlocked=True) as c:
            rel_visible = c.execute(
                "SELECT rel FROM engine_relations WHERE company_id = %s "
                "AND rel = 'approves'",
                (COMPANY,),
            ).fetchall()
        check(
            "f-rls-sql-proof",
            hidden_default == []
            and hidden_cfo_locked == []
            and len(visible_cfo_unlocked) == 1
            and hidden_emp_unlocked == []
            and rel_hidden == []
            and len(rel_visible) == 1,
            f"default={len(hidden_default)} cfo_locked={len(hidden_cfo_locked)} "
            f"cfo_unlocked={len(visible_cfo_unlocked)} "
            f"emp_unlocked={len(hidden_emp_unlocked)} "
            f"rel_hidden={len(rel_hidden)} rel_visible={len(rel_visible)}",
        )

        # (g) ACT still flows for a general topic, recipient from the
        # connect-page stored default (test-only address, patched
        # send — the live Gmail proof is test_act.py).
        from app import gmail_api
        from app.engine import ingest as engine_ingest

        write_fact(conn, COMPANY,
                   "Reimbursement claims must be submitted within 45 days.",
                   visible_to=["employee", "cfo", "cto", "ceo"],
                   source="document")
        conn.commit()
        engine_ingest.ingest_text(
            conn, COMPANY,
            "The HR Lead approves the Reimbursement Policy. "
            "The Reimbursement Policy covers reimbursement claims.",
            "reimbursement-policy.txt", title="Reimbursement Policy",
        )
        conn.commit()
        orig_load = gmail_api.load_recipients
        orig_send = gmail_api.send_via_gmail
        orig_ready = gmail_api.is_gmail_ready
        captured = {}
        gmail_api.load_recipients = lambda *a, **k: {
            "DEFAULT": "test-recipient@example.invalid"
        }
        gmail_api.is_gmail_ready = lambda *a, **k: True

        def _capture_send(to, subject, body, **kwargs):
            captured["to"] = to
            captured["subject"] = subject
            return True, ""

        gmail_api.send_via_gmail = _capture_send
        try:
            reply_g = _answer_text(
                COMPANY, "employee",
                "inform HR Lead about my reimbursement", sender_g,
            )
            audit = conn.execute(
                "SELECT status, recipient FROM email_actions "
                "WHERE company_id = %s ORDER BY created_at DESC LIMIT 1",
                (COMPANY,),
            ).fetchone()
        finally:
            gmail_api.load_recipients = orig_load
            gmail_api.send_via_gmail = orig_send
            gmail_api.is_gmail_ready = orig_ready
        check(
            "g-act-via-gate-recipient",
            reply_g.startswith("Done")
            and captured.get("to") == "test-recipient@example.invalid"
            and audit is not None
            and audit[0] == "sent_gmail",
            f"reply={reply_g[:60]} to={captured.get('to')} audit={audit}",
        )

        # (h) Grep proofs. Precise on purpose, so pre-existing
        # test-only artefacts do not false-positive:
        # - a hardcoded code would be a QUOTED 8-digit string (a
        #   format width like :08d is not a code);
        # - reserved test domains (example.*) are test addresses,
        #   not personal addresses — anything else fails;
        # - client_secret as a variable name (read from the
        #   credentials file) is not a .env auth key; only .env
        #   reads of Gmail/SMTP auth names would fail.
        ok_8, out_8 = grep_no_match(
            r"[\"'][0-9]{8}[\"']",
            ["app/", "scripts/set_auth_code.py", "scripts/test_security.py"],
        )
        check("h-no-8digit-literal", ok_8, f"match={out_8}" if not ok_8 else "")
        proc_mail = subprocess.run(
            ["grep", "-rnE", "--exclude-dir=__pycache__",
             r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
             "app/", "scripts/"],
            cwd=ROOT, capture_output=True, text=True,
        )
        real_mail = [
            ln for ln in proc_mail.stdout.splitlines()
            if not re.search(r"example\.(com|invalid|org|net)", ln)
        ]
        check("h-no-email-literal", not real_mail,
              f"match={real_mail[:1]}" if real_mail else "")
        ok_keys, out_keys = grep_no_match(
            r"getenv\(['\"](GMAIL_|SMTP_)|environ\.get\(['\"](GMAIL_|SMTP_)"
            r"|environ\[['\"](GMAIL_|SMTP_)",
            ["app/"],
        )
        check("h-no-env-auth-keys", ok_keys, f"match={out_keys}" if not ok_keys else "")

        cleanup(conn)
        for sender in (sender_a, sender_b, sender_g):
            conn.execute("DELETE FROM code_unlocks WHERE sender = %s", (sender,))
            conn.execute("DELETE FROM code_attempts WHERE sender = %s", (sender,))
            conn.execute(
                "DELETE FROM pending_questions WHERE sender = %s", (sender,)
            )
        conn.execute("DELETE FROM auth_codes")
        conn.commit()

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
