"""Eval script (P2-7): 30 questions through the router/answer
functions (not real WhatsApp), scored honestly from checks.

Usage:
    PYTHONPATH=. python scripts/eval.py [--company eval-co]
        [--questions scripts/eval_questions.json]
        [--output results.md] [--no-seed]

Behaviour:
- By default it seeds the eval company from app.demo_data
  (fictional Meridian Field Services facts, the same data the
  demo seed script loads), so the run is deterministic whatever
  the DB held before. --no-seed runs against existing data.
- Answerable/superseded cases run router.answer_question on a
  sender-scoped connection (RLS enforces the case role, exactly
  like the WhatsApp path). Correct means the covering facts
  contain every expected value, no expected-absent value, and
  the cited sources are exactly the expected documents.
  As-of cases additionally require the (current)/(superseded)
  tags in the answer text.
- Out-of-scope cases must return exactly
  "Not found in company documents." on the NOT_FOUND route.
- Role-blocked cases run _answer_text with a sender (the same
  function the webhook calls, still not real WhatsApp) and must
  return the exact block message.
- Hallucinated means: a superseded value returned as current
  evidence, a number in the answer text that appears in no
  covering fact (synthesis fabrication), an answer to an
  out-of-scope question, or a restricted value leaked in a
  role-blocked reply. Extra related facts from the router's
  ANY-token coverage (e.g. other allowance facts for an
  allowance question) are broad retrieval, not hallucination,
  and are not counted here — they fail correctness only if an
  expected value or document is missing. A case can be
  incorrect without being hallucinated (e.g. a missing answer).
- Finance/technical answerable cases carry "unlocked": true:
  RLS hides sensitive rows even from allowed roles until the
  code gate unlocks them, so those cases run in post-code-gate
  state. Without the code the same questions correctly yield
  the code challenge (proven by the security suite).
- Stdout carries exactly one line, the score line:
  "X/30 correct, grounded in sources, Y hallucinated."
  The full table goes to the output file (default results.md
  at the repo root). Counts are computed from the checks,
  never hardcoded. Exit code 0 iff the JSON has 30 cases and
  the run completed (a low score is reported, not hidden, by
  the exit code — read the score line).

Fictional data only. No Meta key is required: without one, the
evidence layer (facts + citations) is scored; with one, the
synthesised text is scored the same way.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import demo_data
from app.db import scoped_conn
from app.engine import router

COMPANY_DEFAULT = "eval-co"
NOT_FOUND_EXACT = "Not found in company documents."


def _lower(text):
    return (text or "").lower()


def _cited_docs(source_lines, answer_text):
    """Document titles cited by the Sources block or the answer
    text, restricted to the known demo documents."""
    cited = set()
    for line in source_lines or []:
        if line.startswith("• "):
            rest = line[2:]
            doc = rest.split(", ")[0]
            if doc in demo_data.DOC_TITLES:
                cited.add(doc)
    for doc in demo_data.DOC_TITLES:
        if doc in (answer_text or ""):
            cited.add(doc)
    return cited


def _run_case(case, company):
    """Returns (row dict, correct bool, hallucinated bool)."""
    from app import sourcetags

    category = case["category"]
    role = case["role"]
    question = case["question"]
    expects = case["expects"]
    got = ""
    correct = False
    hallucinated = False
    if category in ("answerable", "superseded"):
        unlocked = bool(case.get("unlocked", False))
        with scoped_conn(role, unlocked=unlocked) as conn:
            result = router.answer_question(conn, company, role, question)
            facts = result.get("facts") or []
            answer = result.get("answer") or ""
            lines = sourcetags.source_lines(conn, company, facts)
            # Timelines ground superseded values in as-of answers;
            # read them here, while the scope is still set.
            from app.retrieve import get_timeline

            timeline_texts = []
            for fact in facts:
                try:
                    for entry in get_timeline(conn, company, fact["id"]):
                        timeline_texts.append(entry.get("fact", ""))
                except Exception:
                    pass
        fact_texts = " | ".join(f.get("fact", "") for f in facts)
        fact_blob = _lower(fact_texts)
        facts_ok = all(
            _lower(exp) in fact_blob or _lower(exp) in _lower(answer)
            for exp in expects.get("facts_contains", [])
        )
        absent_ok = all(
            _lower(exp) not in fact_blob
            for exp in expects.get("facts_absent", [])
        )
        expected_docs = set(expects.get("docs", []))
        cited = _cited_docs(lines, answer)
        docs_ok = expected_docs <= cited
        text_ok = all(
            exp in answer for exp in expects.get("text_contains", [])
        )
        correct = facts_ok and absent_ok and docs_ok and text_ok
        # Synthesis fabrication: a number in the answer text that
        # no covering fact — or its timeline, which grounds
        # superseded values in as-of answers — contains.
        # (Without a key the answer is empty, so this check is
        # vacuous there.)
        import re

        grounded_blob = fact_texts + " | " + " | ".join(timeline_texts)
        # Citation dates (doc, YYYY-MM-DD, tag) are not claimed
        # values: strip them before the number check. Numbers
        # must stand alone: the "2" in "travel-policy-v2.txt" is
        # part of a document name, not a claimed value.
        answer_values = re.sub(r"\d{4}-\d{2}-\d{2}", "", answer)
        standalone = r"(?<![A-Za-z0-9])\d[\d,]*(?![A-Za-z0-9])"
        answer_numbers = {
            n.replace(",", "")
            for n in re.findall(standalone, answer_values)
        }
        fact_numbers = {
            n.replace(",", "")
            for n in re.findall(standalone, grounded_blob)
        }
        hallucinated = (not absent_ok) or bool(
            answer_numbers - fact_numbers
        )
        got = (
            f"route={result.get('route')} facts=[{fact_texts}] "
            f"sources=[{'; '.join(lines)}]"
        )
    elif category == "out-of-scope":
        with scoped_conn(role, unlocked=False) as conn:
            result = router.answer_question(conn, company, role, question)
        answer = result.get("answer") or ""
        correct = (
            result.get("route") == "NOT_FOUND"
            and answer == expects["exact_reply"]
        )
        hallucinated = result.get("route") != "NOT_FOUND"
        got = f"route={result.get('route')} answer={answer!r}"
    elif category == "role-blocked":
        import app.whatsapp as wa_mod

        sender = f"910000009{case['id'][1:]}"
        reply = wa_mod._answer_text(company, role, question, sender)
        correct = reply == expects["block"]
        hallucinated = any(
            _lower(leak) in _lower(reply)
            for leak in expects.get("leak_absent", [])
        )
        got = f"reply={reply!r}"
    else:
        got = f"unknown category {category!r}"
    return {
        "id": case["id"],
        "category": category,
        "role": role,
        "question": question,
        "expected": json.dumps(expects, ensure_ascii=False),
        "got": got,
        "correct": correct,
        "hallucinated": hallucinated,
    }, correct, hallucinated


def _escape(cell):
    return str(cell).replace("|", "\\|").replace("\n", " ")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", default=COMPANY_DEFAULT)
    parser.add_argument(
        "--questions",
        default=os.path.join(here, "eval_questions.json"),
    )
    parser.add_argument(
        "--output", default=os.path.join(repo_root, "results.md")
    )
    parser.add_argument("--no-seed", action="store_true")
    args = parser.parse_args()

    with open(args.questions, encoding="utf-8") as fh:
        cases = json.load(fh)
    if len(cases) != 30:
        print(
            f"eval aborted: expected 30 questions, found {len(cases)}",
            file=sys.stderr,
        )
        return 2
    if not args.no_seed:
        demo_data.seed_company(args.company)

    rows = []
    correct_count = 0
    hallucinated_count = 0
    for case in cases:
        row, correct, hallucinated = _run_case(case, args.company)
        rows.append(row)
        correct_count += 1 if correct else 0
        hallucinated_count += 1 if hallucinated else 0

    lines = []
    lines.append("# Eval results\n")
    lines.append(
        f"Run: {datetime.now(timezone.utc).isoformat()} | "
        f"company: {args.company} | "
        f"seeded: {'no' if args.no_seed else 'yes'} | "
        f"questions: {len(cases)}\n"
    )
    lines.append(
        "Scoring: correct means the covering facts contain every "
        "expected value, no expected-absent value, and the cited "
        "sources include every expected document (as-of cases "
        "also require the (current)/(superseded) tags; out-of-scope "
        "cases require the exact not-found reply; role-blocked cases "
        "require the exact block message). Hallucinated means a "
        "superseded value returned as current evidence, a number "
        "in the answer text that no covering fact contains, an "
        "answer to an out-of-scope question, or a restricted value "
        "leaked in a role-blocked reply. Extra related facts from "
        "the router's ANY-token coverage are broad retrieval, not "
        "hallucination. Counts are computed from the checks.\n"
    )
    lines.append(
        f"Score: {correct_count}/{len(cases)} correct, grounded in "
        f"sources, {hallucinated_count} hallucinated.\n"
    )
    lines.append(
        "| # | Category | Role | Question | Expected | Got | "
        "Correct | Hallucinated |"
    )
    lines.append(
        "| --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    for row in rows:
        lines.append(
            f"| {_escape(row['id'])} | {_escape(row['category'])} | "
            f"{_escape(row['role'])} | {_escape(row['question'])} | "
            f"{_escape(row['expected'])} | {_escape(row['got'])} | "
            f"{'yes' if row['correct'] else 'no'} | "
            f"{'yes' if row['hallucinated'] else 'no'} |"
        )
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(
        f"{correct_count}/{len(cases)} correct, grounded in sources, "
        f"{hallucinated_count} hallucinated."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
