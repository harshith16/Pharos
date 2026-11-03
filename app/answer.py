"""Answering: retrieval + LLM synthesis with an honest template fallback.

Imports app.retrieve and app.llm. app.llm is built in parallel; while it
is absent, synthesis is skipped and the template fallback is used (never
a fabricated answer). Similarity scores are INTERNAL ONLY and are never
put into any output string here.
"""

from app import retrieve

try:  # app.llm built in parallel; absence -> template fallback, not a crash.
    from app import llm
except ImportError:  # pragma: no cover - parallel-build tolerance
    llm = None

EMPTY_ANSWER = "Not found in company documents."


def _build_citations(current, doc_titles=None):
    """One citation per fact: document ids first, else the chat message id."""
    citations = []
    seen = set()
    for fact in current:
        doc_ids = fact.get("document_ids") or []
        if doc_ids:
            for doc_id in doc_ids:
                ref = doc_titles.get(str(doc_id)) if doc_titles else None
                ref = ref or str(doc_id)
                key = (fact["id"], fact["source"], ref)
                if key not in seen:
                    seen.add(key)
                    citations.append(
                        {
                            "memory_id": fact["id"],
                            "source": fact["source"],
                            "source_ref": ref,
                        }
                    )
        elif fact.get("source_message_id"):
            ref = fact["source_message_id"]
            key = (fact["id"], fact["source"], ref)
            if key not in seen:
                seen.add(key)
                citations.append(
                    {
                        "memory_id": fact["id"],
                        "source": fact["source"],
                        "source_ref": ref,
                    }
                )
        else:
            ref = fact["source"]
            key = (fact["id"], fact["source"], ref)
            if key not in seen:
                seen.add(key)
                citations.append(
                    {
                        "memory_id": fact["id"],
                        "source": fact["source"],
                        "source_ref": ref,
                    }
                )
    return citations


def _build_freshness(current):
    if not current:
        return None
    top = current[0]
    if top.get("effective_date"):
        base = f"Current as of {top['effective_date']}"
    else:
        base = "Current policy (no effective date on file)"
    dates = sorted({f["effective_date"] for f in current if f.get("effective_date")})
    if len(dates) > 1:
        base += f" (policies on file span {dates[0]} to {dates[-1]})"
    return base


def _build_conflict(conflict_rows):
    if not conflict_rows:
        return None
    parts = [
        f"Previously {row['fact']}; superseded — see history" for row in conflict_rows
    ]
    return " ".join(parts)


def _template_fallback(current):
    """Honest fallback: restate the top current fact, nothing invented."""
    return current[0]["fact"]


def answer_question(conn, company_id, role, question, alias_map=None):
    current = retrieve.search(
        conn, company_id, role, question, alias_map=alias_map
    )
    # Uploaded-only gate: synthesis may use only facts that actually
    # cover the question. A single weak fact (e.g. the frequency rule
    # for "Rule for sl") must never become the answer.
    current = retrieve.filter_covering(current, question, alias_map)
    all_rows = retrieve.search(
        conn, company_id, role, question, include_superseded=True,
        alias_map=alias_map,
    )
    current_ids = {f["id"] for f in current}
    conflict_rows = [
        r
        for r in all_rows
        if r["status"] == "superseded" and r["id"] not in current_ids
    ]

    if not current:
        return {
            "answer": EMPTY_ANSWER,
            "facts": [],
            "citations": [],
            "freshness": None,
            "conflict": None,
            "empty": True,
        }

    answer = None
    if llm is not None:
        try:
            answer = llm.synthesize(question, current, conflict_rows)
        except Exception:
            answer = None

    else:
        answer = _template_fallback(current)

    if not answer or not str(answer).strip():
        return {
            "answer": EMPTY_ANSWER,
            "facts": [],
            "citations": [],
            "freshness": None,
            "conflict": None,
            "empty": True,
        }

    doc_ids_all = set()
    for f in current:
        for d in (f.get("document_ids") or []):
            doc_ids_all.add(str(d))
    doc_titles = {}
    if doc_ids_all and hasattr(conn, "cursor"):
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, title FROM documents WHERE id = ANY(%s::uuid[])",
                    (list(doc_ids_all),),
                )
                for did, dtitle in cur.fetchall():
                    if dtitle:
                        doc_titles[str(did)] = dtitle
        except Exception:
            pass

    return {
        "answer": answer,
        "facts": current,
        "citations": _build_citations(current, doc_titles=doc_titles),
        "freshness": _build_freshness(current),
        "conflict": _build_conflict(conflict_rows),
        "empty": False,
    }


def format_chat_message(result):
    """Plain chat text: answer, evidence lines, freshness, conflict.

    No markdown tables, no scores, no jargon.
    """
    lines = [result["answer"]]
    citations = result.get("citations") or []
    if citations:
        lines.append("")
        lines.append("Evidence:")
        seen_refs = set()
        for c in citations:
            src = c["source"].capitalize() if isinstance(c.get("source"), str) else "Source"
            ref = c.get("source_ref", "")
            item = f"- {src}: {ref}"
            if item not in seen_refs:
                seen_refs.add(item)
                lines.append(item)
    if result.get("freshness"):
        lines.append("")
        lines.append(result["freshness"])
    if result.get("conflict"):
        lines.append("")
        lines.append(result["conflict"])
    return "\n".join(lines).strip()
