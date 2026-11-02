"""Targeted proofs for the hardening/upload-only-answers branch.

(a) "Rule for sl" returns the Sick Leave facts, never the frequency rule.
(b) A question with no covering uploaded facts returns the not-found
    reply, never a world-knowledge answer.
(c) An image upload with no readable content gets the could-not-be-read
    reply (pure _media_reply decision, no Graph calls).

Run: PYTHONPATH=. DATABASE_URL=<local postgres url> python scripts/test_hardening.py
Exit code 0 iff every check passes. Cleans its own company rows first.
No Meta key is needed: all proofs run against real DB rows with the
model path stubbed out by absence (no key -> no synthesis -> gates).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import retrieve
from app.answer import answer_question
from app.db import get_conn
from app.store import write_fact
from app.whatsapp import (
    MEDIA_NOT_READ_REPLY,
    NOT_FOUND_REPLY,
    _answer_text,
    _media_reply,
)

COMPANY = "hardening-co"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))


def cleanup(conn):
    conn.execute("DELETE FROM edges WHERE company_id = %s", (COMPANY,))
    conn.execute(
        "DELETE FROM memory_sources WHERE memory_id IN "
        "(SELECT id FROM memories WHERE company_id = %s)",
        (COMPANY,),
    )
    conn.execute("DELETE FROM memories WHERE company_id = %s", (COMPANY,))
    conn.commit()


def main():
    with get_conn() as conn:
        cleanup(conn)
        vis = ["employee", "cfo", "cto", "ceo"]
        write_fact(
            conn, COMPANY,
            "Sick Leave (SL) in India provides 6-12 days per year.",
            visible_to=vis, source="document",
            triples=[{"source": "sick_leave", "relationship": "provides",
                      "destination": "6-12 days"}],
        )
        write_fact(
            conn, COMPANY,
            "Sick Leave requires medical proof after 2-3 days.",
            visible_to=vis, source="document",
            triples=[{"source": "sick_leave", "relationship": "requires",
                      "destination": "medical proof"}],
        )
        write_fact(
            conn, COMPANY,
            "The Frequency rule grants 2 paid leaves on monthly basis.",
            visible_to=vis, source="document",
            triples=[{"source": "frequency_rule", "relationship": "grants",
                      "destination": "2 paid leaves"}],
        )

        # (a) The exact failing case.
        rows = retrieve.search(conn, COMPANY, "employee", "Rule for sl")
        top = rows[0]["fact"] if rows else ""
        check(
            "a-sl-top-is-sick-leave",
            "Sick Leave" in top,
            f"top={top[:80]}",
        )
        check(
            "a-frequency-rule-not-top",
            not rows or "Frequency rule" not in rows[0]["fact"],
            f"top={top[:80]}",
        )
        covering = retrieve.filter_covering(rows, "Rule for sl")
        check(
            "a-covering-keeps-sl-drops-frequency",
            all("Frequency rule" not in f["fact"] for f in covering)
            and any("Sick Leave" in f["fact"] for f in covering),
            f"covering={[f['fact'][:40] for f in covering]}",
        )

        # (b) No covering facts -> empty legacy answer -> not-found reply.
        res = answer_question(
            conn, COMPANY, "employee", "What is the rule for holidays?"
        )
        check(
            "b-weak-fact-gated-empty",
            res.get("empty") is True,
            f"answer={str(res.get('answer'))[:60]}",
        )
        res2 = answer_question(
            conn, COMPANY, "employee", "What is the Champions League schedule?"
        )
        check(
            "b-unrelated-empty",
            res2.get("empty") is True,
            f"answer={str(res2.get('answer'))[:60]}",
        )
        reply = _answer_text(COMPANY, "employee", "What is the rule for holidays?")
        check(
            "b-whatsapp-not-found",
            reply == NOT_FOUND_REPLY,
            f"reply={reply[:60]}",
        )
        reply2 = _answer_text(COMPANY, "employee", "What is the Champions League schedule?")
        check(
            "b-whatsapp-not-found-unrelated",
            reply2 == NOT_FOUND_REPLY,
            f"reply={reply2[:60]}",
        )

        # (c) Media reply decisions (pure function, no Graph).
        check(
            "c-image-no-text",
            _media_reply("image", "photo.jpg", None, 0, None) == MEDIA_NOT_READ_REPLY,
        )
        check(
            "c-image-text-no-learning",
            _media_reply("image", "photo.jpg", "hello world", 0, 0) == MEDIA_NOT_READ_REPLY,
        )
        ok_reply = _media_reply("image", "photo.jpg", "hello world", 0, 3)
        check(
            "c-image-with-entities-not-notread",
            ok_reply != MEDIA_NOT_READ_REPLY and "photo.jpg" in ok_reply,
            f"reply={ok_reply[:60]}",
        )
        doc_reply = _media_reply("document", "policy.txt", "Travel limit 9000.", 0, 0)
        check(
            "c-document-with-text-not-notread",
            doc_reply != MEDIA_NOT_READ_REPLY and "policy.txt" in doc_reply,
            f"reply={doc_reply[:60]}",
        )
        cleanup(conn)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
