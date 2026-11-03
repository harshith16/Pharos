"""As-of answers (P0-2): "what was X before" / "what is X now".

Detection is linguistic structure only (like the router's
interrogative set): whole-word markers for past/current phrasing.
It says nothing about the topic — the values always come from
stored version chains (latest + superseded rows), never invented.

Formatting is deterministic and needs no model key: for each
covering fact, its timeline (root-first) gives every version with
its source document, date, and (current)/(superseded) tag.
- Any chain with more than one version: the answer lists those
  versions, newest first, each tagged.
- No chain with history: the single current value, tagged
  current, plus a plain "no earlier value was found" note.
A question with no covering facts never reaches here (the router
returns NOT_FOUND first), so no history is ever fabricated.
"""

import re

_MARKER_RE = re.compile(
    r"\b(before|previously|earlier|previous|now|currently|current)\b"
    r"|\bused\s+to\b"
    r"|\bat\s+present\b",
    re.IGNORECASE,
)


def is_asof_question(question: str) -> bool:
    """True when the message is shaped like an as-of question."""
    return bool(question) and _MARKER_RE.search(question) is not None


def _version_lines(conn, company_id: str, fact_id: str):
    """One covering fact's timeline as (fact, doc, date, tag) lines,
    root-first (oldest first). Tag comes from the stored status:
    'current' for the live version, 'superseded' for older ones."""
    from app.retrieve import get_timeline

    try:
        chain = get_timeline(conn, company_id, fact_id)
    except Exception:
        return []
    if not chain:
        return []
    titles = {}
    try:
        ids = [e["id"] for e in chain]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ms.memory_id, d.title FROM memory_sources ms "
                "JOIN documents d ON d.id = ms.document_id "
                "WHERE ms.memory_id = ANY(%s::uuid[]) "
                "AND ms.document_id IS NOT NULL",
                (ids,),
            )
            for mid, title in cur.fetchall():
                titles.setdefault(str(mid), title)
    except Exception:
        titles = {}
    lines = []
    for entry in chain:
        doc = (
            titles.get(entry["id"])
            or entry.get("source_message_id")
            or entry.get("source")
            or "company documents"
        )
        date = entry.get("effective_date") or ""
        if not date and entry.get("created_at"):
            date = str(entry["created_at"])[:10]
        if not date:
            date = "unknown date"
        tag = "current" if entry.get("status") == "current" else "superseded"
        lines.append((entry["fact"], doc, date, tag))
    return lines


def asof_answer(conn, company_id: str, facts) -> str | None:
    """Deterministic as-of text from covering facts, or None when
    no usable version chain exists (caller keeps its own answer)."""
    if not facts:
        return None
    multi = []
    single = []
    seen = set()
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        fid = fact.get("id")
        if not fid or fid in seen:
            continue
        seen.add(fid)
        lines = _version_lines(conn, company_id, fid)
        if not lines:
            continue
        if len(lines) > 1:
            multi.append(lines)
        else:
            single.append(lines)
    if multi:
        out = []
        for lines in multi:
            for fact_text, doc, date, tag in reversed(lines):
                out.append(f"• {fact_text} — {doc}, {date} ({tag})")
        return "\n".join(out)
    if single:
        out = []
        for lines in single:
            fact_text, doc, date, tag = lines[0]
            out.append(f"• {fact_text} — {doc}, {date} ({tag})")
        out.append("No earlier value was found.")
        return "\n".join(out)
    return None
