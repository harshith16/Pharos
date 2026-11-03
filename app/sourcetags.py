"""Source tags (P0-3): doc name + date + current/superseded on
every factual answer.

Tags are built from stored rows, never typed: the covering fact's
linked document title (else its chat message id, else its source),
its effective date (else its created date), and its stored status
('current' for the live version, 'superseded' for older ones).
As-of answers already carry their own tags, so the router applies
this only to other ENGINE answers — never twice.
"""

import logging

_log = logging.getLogger(__name__)


def source_lines(conn, company_id: str, facts):
    """One '• <doc>, <date> (tag)' line per covering fact, in
    covering order, exact duplicates removed. Never raises."""
    try:
        ids = []
        seen_ids = set()
        by_id = {}
        for fact in facts or []:
            if not isinstance(fact, dict):
                continue
            fid = fact.get("id")
            if not fid or fid in seen_ids:
                continue
            seen_ids.add(fid)
            ids.append(fid)
            by_id[fid] = fact
        if not ids:
            return []
        rows = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT m.id, m.effective_date, m.created_at, m.status, "
                "d.title FROM memories m "
                "LEFT JOIN memory_sources ms "
                "ON ms.memory_id = m.id AND ms.document_id IS NOT NULL "
                "LEFT JOIN documents d ON d.id = ms.document_id "
                "WHERE m.id = ANY(%s::uuid[]) AND m.company_id = %s",
                (ids, company_id),
            )
            for mid, eff, created, status, title in cur.fetchall():
                key = str(mid)
                if key not in rows:
                    rows[key] = (eff, created, status, title)
        lines = []
        seen_lines = set()
        for fid in ids:
            fact = by_id[fid]
            eff, created, status, title = rows.get(fid, (None, None, None, None))
            doc = (
                title
                or fact.get("source_message_id")
                or fact.get("source")
                or "company documents"
            )
            if eff is not None:
                date = eff.isoformat()
            elif created is not None:
                date = str(created)[:10]
            else:
                date = "unknown date"
            if status in ("current", "superseded"):
                tag = status
            elif fact.get("is_latest"):
                tag = "current"
            else:
                tag = "superseded"
            line = f"• {doc}, {date} ({tag})"
            if line not in seen_lines:
                seen_lines.add(line)
                lines.append(line)
        return lines
    except Exception:
        _log.exception("source_lines failed; answer keeps no tags")
        return []


def tag_answer(conn, company_id: str, answer: str, facts) -> str:
    """Append a Sources block to a factual answer, or return the
    answer unchanged when no source lines can be built."""
    lines = source_lines(conn, company_id, facts)
    if not lines:
        return answer
    return answer.rstrip() + "\n\nSources:\n" + "\n".join(lines)
