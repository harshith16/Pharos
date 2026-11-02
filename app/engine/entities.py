"""Entity registry for the company-brain graph.

Own code only: no third-party memory packages, no hosted memory APIs,
no copied code. The LLM is used only through the existing provider-
agnostic client in app.llm (never a hardcoded provider, model, or key),
and only when a key is configured; without a key, extraction is fully
deterministic and works offline.

Write helpers here never call conn.commit(); callers own the
transaction. JSONB values are passed with json.dumps.
"""

import json
import re
import string

from app import llm

# Characters stripped from the ends of a name only (never from inside).
_STRIP_CHARS = string.punctuation + " \t\n\r" + "\u201c\u201d\u2018\u2019"

# Single capitalized words that are never entities on their own, and
# never the first word of a capitalized sequence (sentence starters,
# pronouns, articles, conjunctions). Multi-word sequences keep their
# remaining words; a sequence reduced to nothing is dropped.
_STOPWORDS = {
    "the",
    "a",
    "an",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "he",
    "she",
    "they",
    "we",
    "you",
    "i",
    "when",
    "where",
    "who",
    "what",
    "why",
    "how",
    "and",
    "but",
    "or",
    "if",
    "then",
    "in",
    "on",
    "at",
    "for",
    "with",
    "from",
    "to",
    "of",
    "by",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "has",
    "have",
    "had",
    "will",
    "would",
    "can",
    "could",
    "should",
    "may",
    "might",
    "must",
    "do",
    "does",
    "did",
    "not",
    "no",
    "yes",
    "please",
    "thanks",
    "hello",
    "hi",
}

# A capitalized word is Titlecase or an all-caps token (acronyms).
_CAP_WORD = r"(?:[A-Z][a-z]+|[A-Z]{2,})"
_CAP_SEQ_RE = re.compile(r"\b" + _CAP_WORD + r"(?:\s+" + _CAP_WORD + r")*\b")
_QUOTED_RE = re.compile(
    '"([^"]+)"|“([^”]+)”|\'([^\']+)\'|‘([^’]+)’'
)

_EXTRACT_SYSTEM = (
    "Extract the named entities mentioned in the input text.\n"
    "Rules:\n"
    "- Return ONLY entities that appear in the input text, word for "
    "word. NEVER invent an entity that is not in the input.\n"
    "- Include people, teams, policies, documents, places, and other "
    "proper names.\n"
    "- For each entity, emit its name exactly as written in the input "
    "and a short kind label (for example person, team, policy).\n"
    '- Respond with JSON only: {"entities": [{"name": "...", '
    '"kind": "..."}]}.\n'
)


def normalize_name(name: str) -> str:
    """Lowercase, collapse whitespace, strip surrounding punctuation."""
    if not isinstance(name, str):
        return ""
    collapsed = re.sub(r"\s+", " ", name.lower())
    return collapsed.strip(_STRIP_CHARS)


def _entity_dict(row) -> dict:
    """Build the public dict for one engine_entities row tuple."""
    entity_id, company_id, name, display_name, kind, aliases, created_at, updated_at = row
    return {
        "id": str(entity_id),
        "company_id": company_id,
        "name": name,
        "display_name": display_name,
        "kind": kind,
        "aliases": list(aliases) if aliases is not None else [],
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
    }


_ENTITY_COLUMNS = (
    "id, company_id, name, display_name, kind, aliases, created_at, updated_at"
)


def _find_entity_row(conn, company_id, canonical):
    """Find an entity row by canonical name, then by alias.

    The alias check tries a JSONB containment query first, then falls
    back to a scan that compares normalized aliases, so aliases stored
    in a non-normalized form still match.
    """
    row = conn.execute(
        "SELECT " + _ENTITY_COLUMNS + " FROM engine_entities "
        "WHERE company_id = %s AND name = %s",
        (company_id, canonical),
    ).fetchone()
    if row is not None:
        return row
    row = conn.execute(
        "SELECT " + _ENTITY_COLUMNS + " FROM engine_entities "
        "WHERE company_id = %s AND aliases @> %s::jsonb",
        (company_id, json.dumps([canonical])),
    ).fetchone()
    if row is not None:
        return row
    rows = conn.execute(
        "SELECT " + _ENTITY_COLUMNS + " FROM engine_entities "
        "WHERE company_id = %s",
        (company_id,),
    ).fetchall()
    for candidate in rows:
        aliases = candidate[5] or []
        for alias in aliases:
            if isinstance(alias, str) and normalize_name(alias) == canonical:
                return candidate
        if normalize_name(candidate[3]) == canonical:
            return candidate
    return None


def resolve_entity(conn, company_id, raw_name, kind=None) -> str:
    """Return the id for raw_name, creating the entity if needed.

    Case/whitespace variants merge through normalize_name: an exact
    canonical match wins, then an aliases match, otherwise a new row
    is inserted with display_name = stripped raw name. Never commits.
    """
    canonical = normalize_name(raw_name)
    if not canonical:
        raise ValueError("entity name is empty")
    row = conn.execute(
        "SELECT id FROM engine_entities "
        "WHERE company_id = %s AND name = %s",
        (company_id, canonical),
    ).fetchone()
    if row is not None:
        return str(row[0])
    row = conn.execute(
        "SELECT id FROM engine_entities "
        "WHERE company_id = %s AND aliases @> %s::jsonb",
        (company_id, json.dumps([canonical])),
    ).fetchone()
    if row is not None:
        return str(row[0])
    rows = conn.execute(
        "SELECT id, aliases FROM engine_entities WHERE company_id = %s",
        (company_id,),
    ).fetchall()
    for entity_id, aliases in rows:
        for alias in aliases or []:
            if isinstance(alias, str) and normalize_name(alias) == canonical:
                return str(entity_id)
    display_name = raw_name.strip() if isinstance(raw_name, str) else ""
    row = conn.execute(
        "INSERT INTO engine_entities (company_id, name, display_name, kind, "
        "aliases) VALUES (%s, %s, %s, %s, %s::jsonb) RETURNING id",
        (company_id, canonical, display_name, kind or "entity", json.dumps([])),
    ).fetchone()
    return str(row[0])


def get_entity(conn, company_id, raw_name) -> dict | None:
    """Return the entity dict for raw_name, or None when unknown."""
    canonical = normalize_name(raw_name)
    if not canonical:
        return None
    row = _find_entity_row(conn, company_id, canonical)
    if row is None:
        return None
    return _entity_dict(row)


def merge_entities(conn, company_id, keep_id, drop_id) -> None:
    """Fold drop into keep: aliases move, relations repoint, drop dies.

    Relations that would violate the (company, src, dst, rel) unique
    key after repointing lose to the existing row: the repointed loser
    row is deleted instead of updated. Never commits.
    """
    if str(keep_id) == str(drop_id):
        return None
    keep = conn.execute(
        "SELECT id, name, aliases FROM engine_entities "
        "WHERE id = %s AND company_id = %s",
        (keep_id, company_id),
    ).fetchone()
    drop = conn.execute(
        "SELECT id, name, aliases FROM engine_entities "
        "WHERE id = %s AND company_id = %s",
        (drop_id, company_id),
    ).fetchone()
    if keep is None or drop is None:
        raise ValueError("keep or drop entity not found for this company")
    keep_row_id, keep_name, keep_aliases = keep
    drop_row_id, drop_name, drop_aliases = drop
    merged_aliases = []
    seen = {normalize_name(keep_name)}
    for alias in list(keep_aliases or []) + [drop_name] + list(drop_aliases or []):
        if not isinstance(alias, str):
            continue
        normalized = normalize_name(alias)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged_aliases.append(alias)
    conn.execute(
        "UPDATE engine_entities SET aliases = %s::jsonb, updated_at = now() "
        "WHERE id = %s",
        (json.dumps(merged_aliases), keep_row_id),
    )
    rows = conn.execute(
        "SELECT id, src_id, dst_id, rel FROM engine_relations "
        "WHERE company_id = %s AND (src_id = %s OR dst_id = %s)",
        (company_id, drop_row_id, drop_row_id),
    ).fetchall()
    for rel_id, src_id, dst_id, rel in rows:
        new_src = keep_row_id if str(src_id) == str(drop_row_id) else src_id
        new_dst = keep_row_id if str(dst_id) == str(drop_row_id) else dst_id
        clash = conn.execute(
            "SELECT id FROM engine_relations WHERE company_id = %s "
            "AND src_id = %s AND dst_id = %s AND rel = %s AND id <> %s",
            (company_id, new_src, new_dst, rel, rel_id),
        ).fetchone()
        if clash is not None:
            conn.execute(
                "DELETE FROM engine_relations WHERE id = %s", (rel_id,)
            )
        else:
            conn.execute(
                "UPDATE engine_relations SET src_id = %s, dst_id = %s "
                "WHERE id = %s",
                (new_src, new_dst, rel_id),
            )
    conn.execute(
        "DELETE FROM engine_entities WHERE id = %s AND company_id = %s",
        (drop_row_id, company_id),
    )
    return None


def _clean_llm_entities(entries, text: str) -> list[dict]:
    """Validate LLM entities: grounded in the text, deduped, in order."""
    normalized_text = normalize_name(text)
    cleaned = []
    seen = set()
    for entry in entries:
        if isinstance(entry, str):
            name, kind = entry, "entity"
        elif isinstance(entry, dict):
            name = entry.get("name")
            kind = entry.get("kind") or "entity"
        else:
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        normalized = normalize_name(name)
        if not normalized or normalized in seen:
            continue
        # Groundedness: the normalized name must occur in the
        # normalized text as a whole word sequence, never invented.
        grounded = re.search(
            r"(?<!\w)" + re.escape(normalized) + r"(?!\w)", normalized_text
        )
        if grounded is None:
            continue
        seen.add(normalized)
        if not isinstance(kind, str) or not kind.strip():
            kind = "entity"
        cleaned.append({"name": name.strip(), "kind": kind.strip()})
    return cleaned


def _extract_entities_deterministic(text: str) -> list[dict]:
    """Capitalized word sequences + quoted strings, in text order.

    A single all-caps token in parentheses directly after a
    capitalised word ("Sick Leave (SL)") is NOT an entity: it is the
    short-form spelling of that name, and the alias learner
    (extract_aliases) attaches it to the name's entity instead. Without
    this, the short form would become a bogus standalone entity.
    """
    candidates = []  # (start_offset, name)
    for match in _QUOTED_RE.finditer(text):
        inner = next(group for group in match.groups() if group is not None)
        name = re.sub(r"\s+", " ", inner).strip(_STRIP_CHARS).strip()
        if name:
            candidates.append((match.start(), name))
    for match in _CAP_SEQ_RE.finditer(text):
        tokens = match.group(0).split(" ")
        while len(tokens) > 1 and tokens[0].lower() in _STOPWORDS:
            tokens.pop(0)
        if len(tokens) == 1 and tokens[0].lower() in _STOPWORDS:
            continue
        if not tokens:
            continue
        if (
            len(tokens) == 1
            and tokens[0].isupper()
            and 2 <= len(tokens[0]) <= 6
            and match.start() > 0
            and text[match.start() - 1] == "("
        ):
            # Parenthesised short form after a capitalised word?
            before = text[: match.start() - 1].rstrip()
            prev_word = before.rsplit(" ", 1)[-1] if before else ""
            if prev_word and prev_word[0].isupper():
                continue
        candidates.append((match.start(), " ".join(tokens)))
    candidates.sort(key=lambda item: item[0])
    results = []
    seen = set()
    for _, name in candidates:
        normalized = normalize_name(name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        results.append({"name": name, "kind": "entity"})
    return results


def extract_entities(text: str) -> list[dict]:
    """Extract entities from text; LLM when keyed, deterministic else.

    With an LLM key, the provider-agnostic app.llm.chat_json is asked
    for JSON entities; any failure (no key, error, malformed reply)
    falls back to the deterministic path. Only entities present in
    the text are ever returned. Works offline with no key.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    if llm.has_key():
        try:
            response = llm.chat_json(
                _EXTRACT_SYSTEM,
                "Extract entities from the following text and return JSON "
                '{"entities": [{"name": "...", "kind": "..."}]}:\n\n' + text,
            )
        except Exception:
            return _extract_entities_deterministic(text)
        entities = response.get("entities") if isinstance(response, dict) else None
        if not isinstance(entities, list):
            return _extract_entities_deterministic(text)
        return _clean_llm_entities(entities, text)
    return _extract_entities_deterministic(text)


# A parenthesised short form after a capitalised name: "Sick Leave
# (SL)" teaches the engine that SL is that entity. The name is one or
# more capitalised words; the short form is 2-6 capital letters or
# digits. Sentence starters are excluded via the stopword filter on
# the first word, and a short form equal to a word of the name itself
# is not an alias.
_ALIAS_RE = re.compile(
    r"\b([A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*)*)\s*\(([A-Z0-9]{2,6})\)"
)


def extract_aliases(text: str) -> list[tuple]:
    """(full name, short form) pairs learned from the text itself.

    No hand-typed list exists anywhere: an abbreviation resolves only
    when an upload wrote it in this form. Returns pairs in text order,
    deduped. A name split across a chunk boundary is missed — the
    200-character chunk overlap makes that rare, and a missed alias
    only means that one upload's short form stays unresolved.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    out = []
    seen = set()
    for match in _ALIAS_RE.finditer(text):
        name = re.sub(r"\s+", " ", match.group(1)).strip()
        short = match.group(2)
        if not name:
            continue
        if name.split(" ")[0].lower() in _STOPWORDS:
            continue
        if short.lower() in {w.lower() for w in name.split(" ")}:
            continue
        key = (normalize_name(name), short.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((name, short))
    return out


def add_alias(conn, company_id, entity_id, alias) -> bool:
    """Attach a learned alias to an entity. Returns True when added.

    No-ops when the alias is already attached (normalised compare) or
    when it already belongs to a DIFFERENT entity (alias-collision
    guard: one short form never points at two entities). Never
    commits; the caller owns the transaction.
    """
    if not isinstance(alias, str) or not alias.strip():
        return False
    alias_norm = normalize_name(alias)
    if not alias_norm:
        return False
    row = conn.execute(
        "SELECT id, aliases FROM engine_entities "
        "WHERE id = %s AND company_id = %s",
        (entity_id, company_id),
    ).fetchone()
    if row is None:
        return False
    existing = row[1] or []
    for current in existing:
        if isinstance(current, str) and normalize_name(current) == alias_norm:
            return False
    other = _find_entity_row(conn, company_id, alias_norm)
    if other is not None and str(other[0]) != str(entity_id):
        return False
    conn.execute(
        "UPDATE engine_entities SET aliases = %s::jsonb, updated_at = now() "
        "WHERE id = %s",
        (json.dumps(list(existing) + [alias.strip()]), entity_id),
    )
    return True
