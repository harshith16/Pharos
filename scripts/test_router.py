"""Targeted proofs for the engine-decided router (main).

(a) FRESH upload of a leave-policy text stating "Sick Leave (SL)
    provides 6-12 days per year" and nothing else about SL — then
    "Rule for sl" must route ENGINE and return the sick-leave facts,
    with a grep proof that no hand-typed abbreviation list or greeting
    list remains anywhere in app/.
(b) A factual question with no uploaded coverage must route NOT_FOUND
    and never produce a world-knowledge answer.
(c) A greeting must route LLM_DIRECT with no factual claims.
(d) A question needing a graph hop must route ENGINE with evidence
    from the graph.
(e) A role-hidden fact must route NOT_FOUND for the employee with
    role_blocked signalled, and ENGINE for the CFO.

Notes on what runs without a Meta key on this machine: the legacy
fact rows below stand in for the keyed LLM extraction path (the
production WhatsApp flow dual-writes engine ingest + LLM extraction;
only the LLM call is stubbed here, by absence of a key). Live model
synthesis is therefore NOT proven here — the route decision, the
signals, and the returned facts are. The router returns covering
facts for ENGINE even when synthesis is impossible, so the decision
is testable without a key.

Run: PYTHONPATH=. python scripts/test_router.py
Exit code 0 iff every check passes. Cleans its own company rows first.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_conn
from app.engine import ingest as engine_ingest
from app.engine import router
from app.engine.router import LLM_DIRECT, NOT_FOUND, ENGINE
from app.store import write_fact
from app.whatsapp import NOT_FOUND_REPLY, _answer_text

COMPANY = "router-co"
RESULTS = []
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def grep_no_match(pattern):
    """True when grep finds no match for pattern under app/."""
    proc = subprocess.run(
        ["grep", "-rn", pattern, "app/"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode != 0, proc.stdout.strip()[:200]


def main():
    with get_conn() as conn:
        cleanup(conn)
        vis = ["employee", "cfo", "cto", "ceo"]

        # Fresh engine upload: the ONLY teacher of the SL alias.
        res = engine_ingest.ingest_text(
            conn, COMPANY,
            "Sick Leave (SL) provides 6-12 days per year. "
            "Sick Leave requires medical proof after 2-3 days.",
            "leave-policy.txt",
        )
        check("a-upload-not-duplicate", res.get("duplicate") is False,
              f"entities={res.get('entities')}")
        row = conn.execute(
            "SELECT name, display_name, aliases FROM engine_entities "
            "WHERE company_id = %s",
            (COMPANY,),
        ).fetchall()
        alias_ok = any(
            "sick leave" in (r[0], r[1])
            and any(
                isinstance(a, str) and a.strip().lower() == "sl"
                for a in (r[2] or [])
            )
            for r in row
        )
        check("a-alias-learned-from-upload", alias_ok, f"rows={row}")

        # Legacy fact rows: dual-write stand-in for keyed extraction.
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
        # Graph chain via a second fresh upload (deterministic verbs).
        engine_ingest.ingest_text(
            conn, COMPANY,
            "Priya Sharma manages Rahul Verma. "
            "Rahul Verma approves Travel Policy.",
            "chain.txt",
        )
        # Role-hidden fact: finance only.
        write_fact(
            conn, COMPANY,
            "The Finance Board approves budgets above 50000.",
            visible_to=["cfo", "ceo"], source="document",
            triples=[{"source": "finance_board", "relationship": "approves",
                      "destination": "budgets"}],
        )

        # (a) The exact failing case, decided by data.
        routed = router.route_question(conn, COMPANY, "employee", "Rule for sl")
        facts_text = " ".join(f.get("fact", "") for f in routed.get("facts") or [])
        check(
            "a-sl-routes-engine",
            routed["route"] == ENGINE,
            f"route={routed['route']} signals={routed.get('signals')}",
        )
        check(
            "a-sl-entity-matched-from-alias",
            any("Sick Leave" in e for e in routed["signals"]["entities_matched"]),
            f"matched={routed['signals']['entities_matched']}",
        )
        check(
            "a-sl-facts-returned",
            "6-12" in facts_text and "Frequency rule" not in facts_text,
            f"facts={facts_text[:100]}",
        )
        check(
            "a-alias-expansion-from-data",
            "sl" in routed["signals"]["alias_expansions_used"],
            f"expansions={routed['signals']['alias_expansions_used']}",
        )
        for label, pattern in (
            ("no-abbreviation-list", "ABBREVIATIONS"),
            ("no-greeting-list", "_SMALLTALK"),
            ("no-shortform-literals", '"sl":|"cl":|"el":|"pl":'),
        ):
            ok, out = grep_no_match(pattern)
            check(f"a-grep-{label}", ok, f"match={out}" if not ok else "")

        # (b) No coverage -> NOT_FOUND, never world knowledge.
        for q in (
            "What is the Champions League schedule?",
            "What is the rule for holidays?",
        ):
            r = router.route_question(conn, COMPANY, "employee", q)
            check(
                f"b-notfound-{q[:20]}",
                r["route"] == NOT_FOUND and r["answer"] == NOT_FOUND_REPLY,
                f"route={r['route']} answer={r['answer'][:50]}",
            )
        reply = _answer_text(
            COMPANY, "employee", "What is the Champions League schedule?"
        )
        check(
            "b-whatsapp-notfound",
            reply == NOT_FOUND_REPLY,
            f"reply={reply[:50]}",
        )

        # (c) Greetings -> LLM_DIRECT, no factual claims.
        for greeting in ("hi", "good morning"):
            r = router.route_question(conn, COMPANY, "employee", greeting)
            check(
                f"c-smalltalk-{greeting}",
                r["route"] == LLM_DIRECT,
                f"route={r['route']} signals={r.get('signals')}",
            )

        # (d) Graph hop -> ENGINE with path evidence.
        r = router.route_question(
            conn, COMPANY, "employee",
            "How is Priya Sharma connected to Travel Policy?",
        )
        path = r.get("path") or []
        rels = {step.get("relationship") for step in path}
        check(
            "d-hop-routes-engine",
            r["route"] == ENGINE and len(path) == 2,
            f"route={r['route']} path={r.get('answer', '')[:120]}",
        )
        check(
            "d-hop-evidence",
            rels >= {"manages", "approves"},
            f"rels={rels}",
        )

        # (e) Role signal: hidden from employee, visible to CFO.
        r_emp = router.route_question(
            conn, COMPANY, "employee", "Who approves budgets above 50000?"
        )
        check(
            "e-role-blocked-employee",
            r_emp["route"] == NOT_FOUND
            and r_emp["signals"]["role_blocked"] is True,
            f"route={r_emp['route']} signals={r_emp['signals']}",
        )
        r_cfo = router.route_question(
            conn, COMPANY, "cfo", "Who approves budgets above 50000?"
        )
        cfo_text = " ".join(f.get("fact", "") for f in r_cfo.get("facts") or [])
        check(
            "e-role-allowed-cfo",
            r_cfo["route"] == ENGINE and "budgets" in cfo_text,
            f"route={r_cfo['route']} facts={cfo_text[:80]}",
        )
        cleanup(conn)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
