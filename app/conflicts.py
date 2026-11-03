"""Upload conflict alerts (P0-1): a new upload contradicting a stored
fact is held, not stored, until the uploader replies YES/NO.

Contradiction is data-driven, never a word list: a new fact's triple
(subject, relationship, object) contradicts a stored latest fact
when the stored fact carries an edge with the same subject and
relationship but a DIFFERENT object. Facts without triples cannot
contradict anything detectable, so they are stored normally.

Flow (WhatsApp intake only; /upload keeps its old behaviour):
- _process_media extracts facts, then store_facts_checked stores
  the non-conflicting ones and holds the conflicting ones here.
  Engine ingest is skipped for that upload while anything is held,
  so held content cannot answer questions before YES.
- On YES (resolve_conflict): the held fact is stored superseding
  the old one (old kept, superseded, with timestamps) and the held
  document text is engine-ingested. On NO: nothing was ever stored,
  the held fact is discarded.
- One pending conflict per sender+company. A second conflict
  arriving while one is pending waits as 'queued' and is promoted
  to pending (and messaged) when the first resolves. A second
  upload whose facts all conflict therefore queues; an upload with
  no conflicts proceeds normally even while another conflict is
  pending.

Detection and writes need operator scope (ceo + unlocked): the
edges/memories reads must see restricted rows too, and restricted
held facts must be writable. Callers pass an operator-scoped
connection; store_facts_checked re-applies the scope before each
fact because every write_fact commits, and a commit clears
SET LOCAL variables.
"""

import json
import logging
import re

from app.db import scoped_conn

_log = logging.getLogger("conflicts")

RESOLVED_YES_REPLY = "Done — replaced the stored fact with the new one."
RESOLVED_NO_REPLY = "Kept the stored fact. The new one was discarded."
STALE_REPLY = (
    "That stored fact has already changed since the upload, "
    "so there is nothing to replace."
)


def _norm_edge(value) -> str:
    """Mirror app.store._normalize_edge_value: lower, collapse
    whitespace, strip, spaces become underscores."""
    return re.sub(r"\s+", " ", str(value or "").lower()).strip().replace(" ", "_")


def _norm_rel(value) -> str:
    """Mirror the relationship normalisation in app.store."""
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def _apply_operator_vars(conn):
    """Re-apply ceo + unlocked SET LOCAL (a commit clears them)."""
    conn.execute("SELECT set_config('app.current_role', 'ceo', true)")
    conn.execute("SELECT set_config('app.sensitive_unlocked', 'on', true)")


def _clean_fact_text(text) -> str:
    """Fact text for the alert shape: stripped, with one trailing
    full stop removed so the shape's own full stop does not double."""
    cleaned = (text or "").strip()
    if cleaned.endswith("."):
        cleaned = cleaned[:-1].rstrip()
    return cleaned


def find_conflicts(conn, company_id: str, fact: dict) -> list:
    """Stored latest facts this fact dict contradicts.

    Each returned dict carries memory_id, fact, visible_to,
    effective_date, source, source_message_id. The caller must run
    this on an operator-scoped connection, or restricted stored
    facts stay hidden from the detector. Never raises for bad
    triples — they are skipped.
    """
    out = []
    seen = set()
    for triple in (fact or {}).get("triples") or []:
        if not isinstance(triple, dict):
            continue
        subj = _norm_edge(triple.get("source", triple.get("subject", "")))
        rel = _norm_rel(triple.get("relationship", ""))
        obj = _norm_edge(triple.get("destination", triple.get("object", "")))
        if not subj or not rel or not obj:
            continue
        try:
            rows = conn.execute(
                "SELECT m.id, m.fact, m.visible_to, m.effective_date, "
                "m.source, m.source_message_id "
                "FROM edges e JOIN memories m ON m.id = e.memory_id "
                "WHERE e.company_id = %s AND e.subject = %s "
                "AND e.relationship = %s AND e.object <> %s "
                "AND m.is_latest AND m.status = 'current' "
                "AND NOT m.is_forgotten",
                (company_id, subj, rel, obj),
            ).fetchall()
        except Exception:
            continue
        for row in rows:
            mid = str(row[0])
            if mid in seen:
                continue
            seen.add(mid)
            out.append(
                {
                    "memory_id": mid,
                    "fact": row[1],
                    "visible_to": list(row[2] or []),
                    "effective_date": row[3],
                    "source": row[4],
                    "source_message_id": row[5],
                }
            )
    return out


def old_fact_citation(conn, memory_id: str):
    """(doc_label, date_str) for the alert shape of a stored fact.

    Doc label: the linked document's title when one exists, else
    the chat message id, else the source string. Date: the
    effective date when set, else the created date. Always returns
    a pair — never blank — so the shape is always complete.
    """
    row = conn.execute(
        "SELECT m.effective_date, m.created_at, m.source, "
        "m.source_message_id, d.title "
        "FROM memories m "
        "LEFT JOIN memory_sources ms ON ms.memory_id = m.id "
        "AND ms.document_id IS NOT NULL "
        "LEFT JOIN documents d ON d.id = ms.document_id "
        "WHERE m.id = %s ORDER BY ms.added_at DESC NULLS LAST LIMIT 1",
        (memory_id,),
    ).fetchone()
    if row is None:
        return ("company documents", "unknown date")
    eff, created, source, msg_id, title = row
    doc = title or msg_id or source or "company documents"
    if eff is not None:
        date_str = eff.isoformat() if hasattr(eff, "isoformat") else str(eff)
    elif created is not None:
        date_str = (
            created.date().isoformat()
            if hasattr(created, "date")
            else str(created)
        )
    else:
        date_str = "unknown date"
    return (doc, date_str)


def format_alert(new_fact: str, doc_label: str, date_str: str, old_fact: str) -> str:
    """The exact founder shape for a pending conflict."""
    return (
        f"New doc says {_clean_fact_text(new_fact)}. "
        f"Stored fact ({doc_label}, {date_str}) says "
        f"{_clean_fact_text(old_fact)}. Replace? Reply YES/NO."
    )


def _alert_for_row(conn, row) -> str:
    """Alert text for a pending_conflicts row tuple."""
    _id, old_id, new_fact = row[0], row[1], row[2]
    old = conn.execute(
        "SELECT fact FROM memories WHERE id = %s", (old_id,)
    ).fetchone()
    old_fact = old[0] if old else ""
    doc_label, date_str = old_fact_citation(conn, old_id)
    return format_alert(new_fact, doc_label, date_str, old_fact)


def store_facts_checked(conn, company_id: str, sender: str, doc_text: str,
                        filename: str, document_id, source: str,
                        facts: list):
    """Store non-conflicting facts; hold conflicting ones.

    Returns (stored_ids, alert_or_None, queued_count). `alert` is
    the exact-shape message for the conflict that became pending
    (None when nothing became pending); `queued_count` counts the
    conflicts that queued behind an existing pending one. The
    caller skips engine ingest for this upload when alert is set
    or queued_count > 0 — held content must not answer until YES.
    """
    from app.store import write_fact

    stored_ids = []
    held = []  # (old_fact_dict, fact_dict) in upload order
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        fact_text = fact.get("fact")
        if not isinstance(fact_text, str) or not fact_text.strip():
            continue
        try:
            _apply_operator_vars(conn)
            found = find_conflicts(conn, company_id, fact)
        except Exception:
            found = []
        if found:
            held.append((found[0], fact))
            continue
        try:
            _apply_operator_vars(conn)
            sid = write_fact(
                conn,
                company_id,
                fact=fact_text,
                visible_to=fact.get("visible_to")
                or ["employee", "cfo", "cto", "ceo"],
                effective_date=fact.get("effective_date"),
                source=source,
                document_id=document_id,
                triples=fact.get("triples"),
            )
            stored_ids.append(sid)
        except Exception:
            _log.exception("storing non-conflicting fact failed")
            continue
    if not held:
        return stored_ids, None, 0
    _apply_operator_vars(conn)
    has_pending = conn.execute(
        "SELECT 1 FROM pending_conflicts WHERE company_id = %s "
        "AND sender = %s AND status = 'pending'",
        (company_id, sender),
    ).fetchone() is not None
    alert = None
    queued = 0
    for index, (old, fact) in enumerate(held):
        status = "pending" if (index == 0 and not has_pending) else "queued"
        conn.execute(
            "INSERT INTO pending_conflicts (company_id, sender, "
            "old_memory_id, new_fact, new_triples, new_visible_to, "
            "new_effective_date, new_source, new_document_id, "
            "new_doc_text, new_filename, status) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)",
            (
                company_id,
                sender,
                old["memory_id"],
                fact.get("fact"),
                json.dumps(fact.get("triples") or []),
                list(
                    fact.get("visible_to")
                    or ["employee", "cfo", "cto", "ceo"]
                ),
                fact.get("effective_date"),
                source,
                document_id,
                doc_text or "",
                filename or "",
                status,
            ),
        )
        if status == "pending":
            doc_label, date_str = old_fact_citation(conn, old["memory_id"])
            alert = format_alert(
                fact.get("fact"), doc_label, date_str, old["fact"]
            )
        else:
            queued += 1
    conn.commit()
    return stored_ids, alert, queued


def resolve_conflict(company_id: str, sender: str, yes: bool):
    """Resolve the sender's pending conflict. Returns
    (reply, next_alert) or None when nothing is pending.

    YES stores the held fact superseding the old one and
    engine-ingests the held document text; NO discards the held
    fact (nothing was ever stored). Afterwards the oldest queued
    conflict for the same sender+company, if any, becomes pending
    and its alert is returned as next_alert.
    """
    from app.store import write_fact

    with scoped_conn("ceo", unlocked=True) as conn:
        row = conn.execute(
            "SELECT id, old_memory_id, new_fact, new_triples, "
            "new_visible_to, new_effective_date, new_source, "
            "new_document_id, new_doc_text, new_filename "
            "FROM pending_conflicts WHERE company_id = %s AND sender = %s "
            "AND status = 'pending' ORDER BY created_at LIMIT 1",
            (company_id, sender),
        ).fetchone()
        if row is None:
            return None
        (pid, old_id, new_fact, new_triples, new_vis, new_eff,
         new_source, new_doc_id, new_doc_text, new_filename) = row
        old = conn.execute(
            "SELECT is_latest, status FROM memories WHERE id = %s",
            (old_id,),
        ).fetchone()
        old_current = bool(
            old and old[0] is True and old[1] == "current"
        )
        if yes and old_current:
            try:
                triples = new_triples
                if isinstance(triples, str):
                    triples = json.loads(triples)
                _apply_operator_vars(conn)
                write_fact(
                    conn,
                    company_id,
                    fact=new_fact,
                    visible_to=list(new_vis or ["employee"]),
                    effective_date=new_eff,
                    source=new_source or "document",
                    document_id=new_doc_id,
                    triples=triples or [],
                    supersedes_id=old_id,
                )
            except Exception:
                _log.exception("storing held fact on YES failed")
                conn.execute(
                    "UPDATE pending_conflicts SET status = 'stale' "
                    "WHERE id = %s",
                    (pid,),
                )
                conn.commit()
                return (STALE_REPLY, None)
            _apply_operator_vars(conn)
            try:
                from app.engine import ingest as engine_ingest

                engine_ingest.ingest_text(
                    conn, company_id, new_doc_text or new_fact,
                    new_filename or "held-document.txt",
                    title=new_filename or None,
                )
            except Exception:
                _log.exception("engine ingest of held doc failed on YES")
            conn.execute(
                "UPDATE pending_conflicts SET status = 'resolved_yes' "
                "WHERE id = %s",
                (pid,),
            )
            reply = RESOLVED_YES_REPLY
        elif yes:
            conn.execute(
                "UPDATE pending_conflicts SET status = 'stale' "
                "WHERE id = %s",
                (pid,),
            )
            reply = STALE_REPLY
        else:
            conn.execute(
                "UPDATE pending_conflicts SET status = 'resolved_no' "
                "WHERE id = %s",
                (pid,),
            )
            reply = RESOLVED_NO_REPLY
        # Promote the oldest queued conflict, if any.
        next_alert = None
        nxt = conn.execute(
            "SELECT id, old_memory_id, new_fact FROM pending_conflicts "
            "WHERE company_id = %s AND sender = %s AND status = 'queued' "
            "ORDER BY created_at LIMIT 1",
            (company_id, sender),
        ).fetchone()
        if nxt is not None:
            conn.execute(
                "UPDATE pending_conflicts SET status = 'pending' "
                "WHERE id = %s",
                (nxt[0],),
            )
            _apply_operator_vars(conn)
            next_alert = _alert_for_row(conn, nxt)
        conn.commit()
        return reply, next_alert
