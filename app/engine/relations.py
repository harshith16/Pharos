"""Typed relations between entities for the company-brain graph.

Own code only: no third-party memory packages, no hosted memory APIs,
no copied code. This module imports app.engine.entities and nothing in
entities imports this module, so the dependency runs one direction.

Write helpers here never call conn.commit(); callers own the
transaction. JSONB values are passed with json.dumps.
"""

import json
import re
from collections import deque

from app.engine import entities
from app.engine.entities import extract_entities as _extract_entities
from app.engine.entities import normalize_name

# Single-token verbs mapped to their relation type. Any other verb
# token found between two mentions maps to 'mentioned_with'.
VERB_MAP = {
    "manages": "manages",
    "approves": "approves",
    "sets": "sets",
    "covers": "covers",
    "owns": "owns",
    "leads": "leads",
    "contains": "contains",
    "includes": "includes",
    "uses": "uses",
    "requires": "requires",
}

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD_RE = re.compile(r"[A-Za-z]+")


def _clean_evidence(evidence) -> list[str]:
    """Normalize evidence to a list of non-empty strings, in order."""
    if evidence is None:
        return []
    if isinstance(evidence, str):
        items = [evidence]
    elif isinstance(evidence, (list, tuple)):
        items = list(evidence)
    else:
        items = [evidence]
    cleaned = []
    for item in items:
        text = str(item)
        if text:
            cleaned.append(text)
    return cleaned


def _normalize_rel(rel) -> str:
    """Lowercase, strip, and collapse whitespace on a relation type."""
    if rel is None:
        return ""
    return " ".join(str(rel).strip().lower().split())


def add_relation(conn, company_id, src_name, rel, dst_name, evidence=None) -> str:
    """Upsert one typed relation between two entities; return its id.

    Both endpoint names resolve via entities.resolve_entity. On a
    unique-key conflict, new evidence ids merge into evidence JSONB
    deduped, and weight becomes the evidence count (minimum 1.0).
    Never commits.
    """
    rel_norm = _normalize_rel(rel)
    if not rel_norm:
        raise ValueError("relation type is empty")
    src_id = entities.resolve_entity(conn, company_id, src_name)
    dst_id = entities.resolve_entity(conn, company_id, dst_name)
    new_evidence = _clean_evidence(evidence)
    row = conn.execute(
        "SELECT id, evidence FROM engine_relations WHERE company_id = %s "
        "AND src_id = %s AND dst_id = %s AND rel = %s",
        (company_id, src_id, dst_id, rel_norm),
    ).fetchone()
    if row is not None:
        rel_id, existing = row
        merged = []
        for item in list(existing or []) + new_evidence:
            text = str(item)
            if text and text not in merged:
                merged.append(text)
        weight = max(1.0, float(len(merged)))
        conn.execute(
            "UPDATE engine_relations SET evidence = %s::jsonb, weight = %s "
            "WHERE id = %s",
            (json.dumps(merged), weight, rel_id),
        )
        return str(rel_id)
    weight = max(1.0, float(len(new_evidence)))
    row = conn.execute(
        "INSERT INTO engine_relations (company_id, src_id, dst_id, rel, "
        "evidence, weight) VALUES (%s, %s, %s, %s, %s::jsonb, %s) "
        "RETURNING id",
        (company_id, src_id, dst_id, rel_norm, json.dumps(new_evidence), weight),
    ).fetchone()
    return str(row[0])


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences on terminal punctuation / newlines."""
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]


def _mention_spans(sentence: str, names: list[str]) -> list[tuple]:
    """Entity mentions in a sentence, in order of appearance.

    Matching is by normalized name: case-insensitive, with any run of
    spaces/underscores standing for a separator. Overlapping matches
    keep the longest match at the earliest start; the rest are dropped.
    Returns [(start, end, display_name), ...] sorted by start.
    """
    spans = []
    for name in names:
        normalized = normalize_name(name)
        if not normalized:
            continue
        pattern = re.compile(
            r"(?<!\w)"
            + r"[\s_]+".join(
                re.escape(token) for token in re.split(r"[\s_]+", normalized)
            )
            + r"(?!\w)",
            re.IGNORECASE,
        )
        for match in pattern.finditer(sentence):
            spans.append((match.start(), match.end(), name))
    spans.sort(key=lambda span: (span[0], -(span[1] - span[0])))
    accepted = []
    for span in spans:
        if any(
            span[0] < end and start < span[1] for start, end, _ in accepted
        ):
            continue
        accepted.append(span)
    accepted.sort(key=lambda span: span[0])
    return accepted


def extract_relations(text, entities=None) -> list[dict]:
    """Extract relations between entities co-occurring in a sentence.

    Entities come from the entities argument (dicts with a name key,
    or plain name strings) or, when None, from extract_entities(text).
    For each sentence, each adjacent pair of mentions in order of
    appearance yields one relation: the first VERB_MAP verb token
    between the two mentions, else 'mentioned_with'. Only pairs that
    actually co-occur in a sentence are returned.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    if entities is None:
        entity_names = []
        for entry in _extract_entities(text):
            if isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str) and name.strip():
                    entity_names.append(name)
    else:
        entity_names = []
        for entry in entities:
            if isinstance(entry, dict):
                name = entry.get("name")
            elif isinstance(entry, str):
                name = entry
            else:
                continue
            if isinstance(name, str) and name.strip():
                entity_names.append(name)
    results = []
    seen = set()
    for sentence in _split_sentences(text):
        spans = _mention_spans(sentence, entity_names)
        for (_, first_end, source), (second_start, _, target) in zip(
            spans, spans[1:]
        ):
            between = sentence[first_end:second_start]
            relationship = "mentioned_with"
            for token in _WORD_RE.findall(between.lower()):
                if token in VERB_MAP:
                    relationship = VERB_MAP[token]
                    break
            key = (
                normalize_name(source),
                relationship,
                normalize_name(target),
            )
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "source": source,
                    "relationship": relationship,
                    "target": target,
                }
            )
    return results


def sync_from_edges(conn, company_id) -> dict:
    """Materialize legacy edges rows as entities + relations.

    Every edges row for the company resolves its subject/object as
    entities and upserts the relation with evidence=[str(memory_id)].
    Counts only genuinely new entities and relations. Never commits.
    """
    rows = conn.execute(
        "SELECT memory_id, subject, relationship, object FROM edges "
        "WHERE company_id = %s ORDER BY created_at",
        (company_id,),
    ).fetchall()
    entities_created = 0
    relations_created = 0
    for memory_id, subject, relationship, obj in rows:
        if not subject or not str(subject).strip():
            continue
        if not obj or not str(obj).strip():
            continue
        if not relationship or not str(relationship).strip():
            continue
        rel_norm = _normalize_rel(relationship)
        if not rel_norm:
            continue
        for name in (subject, obj):
            if entities.get_entity(conn, company_id, name) is None:
                entities.resolve_entity(conn, company_id, name)
                entities_created += 1
        src = entities.get_entity(conn, company_id, subject)
        dst = entities.get_entity(conn, company_id, obj)
        existing = conn.execute(
            "SELECT id FROM engine_relations WHERE company_id = %s "
            "AND src_id = %s AND dst_id = %s AND rel = %s",
            (company_id, src["id"], dst["id"], rel_norm),
        ).fetchone()
        add_relation(
            conn,
            company_id,
            subject,
            rel_norm,
            obj,
            evidence=[str(memory_id)],
        )
        if existing is None:
            relations_created += 1
    return {
        "entities_created": entities_created,
        "relations_created": relations_created,
    }


def traverse(conn, company_id, start_name, max_hops=4) -> list[dict]:
    """Breadth-first paths out of start_name, both directions.

    Each result is {"path": [steps], "score": (sum weights) / hops}.
    A forward step follows a stored relation as-is; a reverse step
    swaps from/to and sets "reversed": True. Paths never revisit a
    node, are capped at max_hops steps, and sort by score desc then
    hops asc. Returns [] when the start entity is unknown.
    """
    if max_hops is None or max_hops < 1:
        return []
    start = entities.get_entity(conn, company_id, start_name)
    if start is None:
        return []
    entity_rows = conn.execute(
        "SELECT id, display_name FROM engine_entities WHERE company_id = %s",
        (company_id,),
    ).fetchall()
    names = {str(entity_id): display for entity_id, display in entity_rows}
    relation_rows = conn.execute(
        "SELECT src_id, dst_id, rel, weight FROM engine_relations "
        "WHERE company_id = %s",
        (company_id,),
    ).fetchall()
    adjacency: dict[str, list[tuple]] = {}
    for src_id, dst_id, rel, weight in relation_rows:
        step_weight = float(weight) if weight is not None else 1.0
        adjacency.setdefault(str(src_id), []).append(
            (str(dst_id), rel, step_weight, False)
        )
        adjacency.setdefault(str(dst_id), []).append(
            (str(src_id), rel, step_weight, True)
        )
    start_id = str(start["id"])
    results = []
    queue = deque()
    queue.append((start_id, [], 0.0, {start_id}))
    while queue:
        node, path, total_weight, visited = queue.popleft()
        if len(path) >= max_hops:
            continue
        for next_id, rel, step_weight, reversed_step in adjacency.get(node, []):
            if next_id in visited:
                continue
            step = {
                "from": names.get(node, node),
                "relationship": rel,
                "to": names.get(next_id, next_id),
                "reversed": reversed_step,
            }
            new_path = path + [step]
            hops = len(new_path)
            new_total = total_weight + step_weight
            results.append({"path": new_path, "score": new_total / hops})
            queue.append((next_id, new_path, new_total, visited | {next_id}))
    results.sort(key=lambda entry: (-entry["score"], len(entry["path"])))
    return results


def find_path(conn, company_id, start_name, end_name, max_hops=4) -> dict | None:
    """Best-scoring traverse path whose final node is end_name.

    The final node is matched by entity identity (so an alias of the
    end entity also matches), in either traversal direction. Returns
    None when either endpoint is unknown or no path connects them.
    """
    end = entities.get_entity(conn, company_id, end_name)
    if end is None:
        return None
    end_display = end["display_name"]
    end_normalized = normalize_name(end_display)
    for entry in traverse(conn, company_id, start_name, max_hops=max_hops):
        path = entry.get("path") or []
        if not path:
            continue
        final = path[-1].get("to")
        if final == end_display or normalize_name(final) == end_normalized:
            return entry
    return None
