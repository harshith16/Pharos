"""GB-scale streaming ingest for the company brain (app/engine/).

Design rules enforced here:

- NEVER load a whole file into memory. Every read carries an explicit
  size (at most 64 KiB per read call); there is no bare read(), no
  whole-file readlines(), and no Path.read_bytes()/read_text() on the
  input. The chunk buffer stays bounded: after each block is drained
  it holds less than one chunk, so peak size is under
  chunk_chars + one 64 KiB block.
- NEVER store a whole file's text in one row. The documents row is
  written with content='' (empty); the full text lives only in chunks
  rows, one per window. chunk_count is filled in at the end.
- NO facts are created here. Fact extraction stays with the existing
  LLM path. This module writes chunks, entities, and relations only.
- Vectors are never faked. app.embed.embed_document() returning None
  (Ollama absent, model missing, over-long input) stores NULL in the
  embedding column, never a placeholder vector.

Two-pass design (ingest_file), and why it costs two passes:

  Hashing requires a full pass over the content, but a duplicate must
  write NOTHING -- and batched commits during that pass would make a
  rollback impossible, because committed rows cannot be rolled back.
  So ingest_file does:

    Pass 1 -- hash only: iterate chunk_stream with no DB writes,
      feeding the hasher the de-overlapped text (first window whole,
      each later window with its overlap prefix stripped, so the hash
      is exactly SHA-256 of the full decoded text). Count windows.
    Dedup check: dedup.register_upload() on that hash. On duplicate,
      return immediately; zero rows have been written.
    Pass 2 -- writes only: re-open the source and iterate
      chunk_stream again, inserting the documents row, one chunks row
      per window, and per-chunk entities/relations, committing every
      batch_commit chunks and once at the end. If pass 2 yields a
      different window count than pass 1 (the file changed between
      passes), the transaction is rolled back and an error raised
      rather than storing chunks that do not match the hash.

  The source must therefore be re-readable. ingest_file accepts only
  a filesystem path (str/Path, opened once per pass) or a bytes object
  (the caller already holds it in memory, so hashing it directly and
  chunking from an in-memory stream costs no extra file load; bytes
  input is the caller's responsibility, documented at ingest_file).
  A generic one-shot stream is deliberately NOT accepted: it cannot
  be rewound for pass 2, and silently falling back to buffering it
  would break the never-load-a-whole-file rule.

  ingest_text is the small-input convenience wrapper: the text is
  already in memory (tests, chat-sized input), so it hashes first,
  dedup-checks, and only then writes -- no second pass is needed.

Sibling modules (built in parallel) are imported lazily, inside the
functions that need them, so importing this module -- and using
chunk_stream standalone -- never requires them to exist yet. Verified
against the landed siblings (not guesses):
  app.engine.dedup.register_upload(conn, company_id, content_hash,
      filename) -> (is_new, id); never commits (caller owns the txn).
  app.engine.entities.extract_entities(text) -> [{"name", "kind"}];
      resolve_entity(conn, company_id, raw_name, kind=None) -> str id;
      get_entity(conn, company_id, name) -> dict | None.
  app.engine.relations.extract_relations(text)
      -> [{"source", "relationship", "target"}];
      add_relation(conn, company_id, src_name, rel, dst_name,
      evidence=None) -> str id (endpoints are NAMES, resolved by the
      sibling; passing a UUID string as a name would create a bogus
      entity, so id-only endpoints are skipped, never passed through).
Return shapes are still normalised defensively (see _is_new_result)
for a (id, is_new) pair, a dict flag, or a bare id, so a sibling
returning newness information is honoured; but with the landed
siblings, entity newness is exact via get_entity (existing by name
or alias => not counted) and relation newness is exact via a
pre-add SELECT on (company_id, src_id, dst_id, rel).
"""

import codecs
import hashlib
import inspect
import io
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

from app.embed import embed_document

__all__ = ["chunk_stream", "ingest_file", "ingest_text"]

# Hard bound on every single read call (bytes for binary streams,
# characters for text streams). Never call read() without a size.
_BLOCK_SIZE = 65536

_DEFAULT_CHUNK_CHARS = 4000
_DEFAULT_OVERLAP = 200
_DEFAULT_BATCH_COMMIT = 100


# ---------------------------------------------------------------------------
# Streaming chunker
# ---------------------------------------------------------------------------

def chunk_stream(stream, chunk_chars: int = _DEFAULT_CHUNK_CHARS,
                 overlap: int = _DEFAULT_OVERLAP) -> Iterator[tuple[int, str]]:
    """Yield (char_offset, text) windows of chunk_chars with overlap.

    stream is a binary file-like object (read() returns bytes) or a
    text file-like object (read() returns str). The mode is detected
    from the first block actually read, so binary streams are decoded
    with an incremental UTF-8 decoder: a multi-byte character split
    across two 64 KiB blocks decodes correctly, and no block is ever
    decoded twice.

    This is a true generator: nothing is read until the caller pulls
    the first window, and at most one block plus the pending window
    tail is held at any time. Windows advance by
    (chunk_chars - overlap) characters, so consecutive windows share
    exactly `overlap` characters and de-overlapping reconstructs the
    input exactly: first window whole, then each later window with
    its first `overlap` characters stripped.

    An empty stream yields nothing. A stream shorter than chunk_chars
    yields one window at offset 0. The final window is whatever tail
    remains (at least the overlap tail of the previous window, unless
    the input ended exactly on a window boundary, in which case there
    is no extra tail window).
    """
    if not isinstance(chunk_chars, int) or isinstance(chunk_chars, bool) \
            or chunk_chars <= 0:
        raise ValueError("chunk_chars must be a positive int")
    if not isinstance(overlap, int) or isinstance(overlap, bool) \
            or overlap < 0 or overlap >= chunk_chars:
        raise ValueError("overlap must satisfy 0 <= overlap < chunk_chars")
    if not hasattr(stream, "read"):
        raise TypeError("stream must be a file-like object with read()")
    return _chunk_stream_impl(stream, chunk_chars, overlap)


def _chunk_stream_impl(stream, chunk_chars: int,
                       overlap: int) -> Iterator[tuple[int, str]]:
    step = chunk_chars - overlap
    buf = ""          # pending decoded text not yet emitted
    buf_start = 0     # char offset of buf[0] in the full text
    decoder = None    # incremental UTF-8 decoder, binary mode only
    binary = None     # decided by the type of the first block read

    while True:
        block = stream.read(_BLOCK_SIZE)
        if block is None:
            # Some file-like objects return None instead of b''/''.
            block = b"" if binary is not False else ""
        if binary is None:
            if isinstance(block, bytes):
                binary = True
                decoder = codecs.getincrementaldecoder("utf-8")()
            elif isinstance(block, str):
                binary = False
            else:
                raise TypeError(
                    "stream.read() must return bytes or str, got "
                    f"{type(block).__name__}"
                )
        if binary:
            text = decoder.decode(block, final=False)
        else:
            text = block
        buf += text
        if not block:
            # End of stream: flush a split trailing character, if any.
            if binary:
                buf += decoder.decode(b"", final=True)
            break
        while len(buf) >= chunk_chars:
            yield (buf_start, buf[:chunk_chars])
            buf = buf[step:]
            buf_start += step
    if buf:
        yield (buf_start, buf)


def _deoverlapped_hash_and_count(stream, chunk_chars: int,
                                 overlap: int) -> tuple[str, int]:
    """SHA-256 (hex) of the full decoded text plus the window count.

    Feeds the hasher only new characters: the first window whole,
    each later window with its overlap prefix stripped. This makes
    the digest exactly SHA-256 of the input text's UTF-8 encoding
    (identical to the raw file bytes for valid UTF-8 input), even
    though chunk_stream itself yields overlapping windows. No DB
    writes happen here; this is pass 1 of ingest_file.
    """
    hasher = hashlib.sha256()
    count = 0
    for _offset, window in chunk_stream(stream, chunk_chars, overlap):
        if count == 0:
            hasher.update(window.encode("utf-8"))
        else:
            hasher.update(window[overlap:].encode("utf-8"))
        count += 1
    return hasher.hexdigest(), count


# ---------------------------------------------------------------------------
# Sibling helpers (lazy imports: this module must import before they exist)
# ---------------------------------------------------------------------------

def _engine_helpers():
    """Import sibling engine helpers, or raise a clear error naming them.

    Called only on the DB-writing paths. chunk_stream and the hashing
    pass never need these imports.
    """
    try:
        from app.engine import dedup, entities, relations
    except ImportError as exc:
        raise RuntimeError(
            "app.engine sibling modules are required for ingest "
            "(app.engine.dedup, app.engine.entities, app.engine.relations) "
            f"but could not be imported: {exc}"
        ) from exc
    return dedup, entities, relations


def _is_new_result(result) -> bool | None:
    """Extract an is_new flag from a sibling helper's return value.

    Returns True/False when the value carries one (dict key is_new /
    created / new, object attribute of the same names, or the second
    element of a (id, is_new) pair); None when it carries no newness
    information (a bare id), in which case callers fall back to
    first-seen-in-this-call counting.
    """
    if isinstance(result, (tuple, list)) and len(result) == 2 \
            and isinstance(result[1], bool):
        return result[1]
    if isinstance(result, dict):
        for key in ("is_new", "created", "new"):
            if isinstance(result.get(key), bool):
                return result[key]
        return None
    for attr in ("is_new", "created", "new"):
        value = getattr(result, attr, None)
        if isinstance(value, bool):
            return value
    return None


def _result_id(result):
    """Extract the id from a sibling helper's return value."""
    if isinstance(result, (tuple, list)) and len(result) >= 1:
        return result[0]
    if isinstance(result, dict):
        for key in ("id", "entity_id", "relation_id"):
            if result.get(key) is not None:
                return result[key]
        return None
    return result


def _is_new_register(result) -> bool:
    """Extract is_new from dedup.register_upload's return value.

    Accepts a dict with is_new, an object with an is_new attribute, a
    (is_new, ...) tuple, or a bare bool. An unrecognised shape raises
    instead of guessing: guessing wrong here could write duplicate
    rows (or drop a new upload).
    """
    flag = _is_new_result(result)
    if flag is not None:
        return flag
    if isinstance(result, bool):
        return result
    if isinstance(result, (tuple, list)) and len(result) >= 1 \
            and isinstance(result[0], bool):
        return result[0]
    raise RuntimeError(
        "dedup.register_upload returned an unrecognised value "
        f"(expected an is_new flag, got {type(result).__name__}); "
        "refusing to guess whether this upload is a duplicate"
    )


def _call_register_upload(dedup, conn, company_id, content_hash, filename):
    """Call register_upload, tolerating its argument order variants.

    Preferred shape is (conn, company_id, content_hash, filename).
    On TypeError, retries with (conn, company_id, filename,
    content_hash) before giving up, so a sibling that ordered the
    trailing arguments differently still integrates.
    """
    register = dedup.register_upload
    try:
        return register(conn, company_id, content_hash, filename)
    except TypeError:
        return register(conn, company_id, filename, content_hash)


def _safe_embed(title: str, text: str):
    """embed_document wrapper: None or over-long input => None (NULL).

    embed_document raises ValueError when its input exceeds the
    model token budget; that must degrade to NULL, exactly as the
    existing store layer treats it, never to a fake vector.
    """
    try:
        return embed_document(title, text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Entity / relation normalisation
# ---------------------------------------------------------------------------

def _entity_fields(item) -> dict:
    """Normalise one extract_entities item to {name, display_name, kind}."""
    if isinstance(item, str):
        return {"name": item, "display_name": item, "kind": "entity"}
    if isinstance(item, dict):
        name = item.get("name") or item.get("display_name") or item.get("text")
        return {
            "name": name,
            "display_name": item.get("display_name") or name,
            "kind": item.get("kind") or "entity",
        }
    name = getattr(item, "name", None) or getattr(item, "display_name", None)
    return {
        "name": name,
        "display_name": getattr(item, "display_name", None) or name,
        "kind": getattr(item, "kind", None) or "entity",
    }


def _relation_fields(item) -> tuple:
    """Normalise one extract_relations item to (src, dst, rel).

    Dict keys tried, in order: src/src_id/source/subject/from,
    dst/dst_id/destination/object/to, rel/relationship/relation/type.
    A 3-tuple is read as (src, rel, dst). src/dst may be names,
    entity dicts/objects, or already-resolved ids.
    """
    if isinstance(item, dict):
        src = None
        for key in ("src", "src_id", "source", "subject", "from"):
            if item.get(key) is not None:
                src = item[key]
                break
        dst = None
        for key in ("dst", "dst_id", "destination", "object", "to",
                    "target", "target_id"):
            if item.get(key) is not None:
                dst = item[key]
                break
        rel = None
        for key in ("rel", "relationship", "relation", "type"):
            if item.get(key) is not None:
                rel = item[key]
                break
        return (src, dst, rel)
    if isinstance(item, (tuple, list)) and len(item) == 3:
        src, rel, dst = item
        return (src, dst, rel)
    raise ValueError(f"unrecognised relation item: {item!r}")


def _call_resolve_entity(entities_mod, conn, company_id, fields: dict):
    """Resolve one entity, adapting to the sibling's parameter shape.

    The real sibling signature is resolve_entity(conn, company_id,
    raw_name, kind=None): a parameter named raw_name or name receives
    the name string (plus kind= when accepted). A parameter named
    entity / ent / data / item receives the fields dict instead.
    Falls back to trying the dict shape, then the name shape.
    """
    resolve = entities_mod.resolve_entity
    try:
        names = [p.name for p in
                 inspect.signature(resolve).parameters.values()]
    except (TypeError, ValueError):
        names = []
    if any(n in names for n in ("entity", "ent", "data", "item")):
        return resolve(conn, company_id, fields)
    if "raw_name" in names or "name" in names:
        kwargs = {}
        if "kind" in names:
            kwargs["kind"] = fields["kind"]
        if "display_name" in names:
            kwargs["display_name"] = fields["display_name"]
        return resolve(conn, company_id, fields["name"], **kwargs)
    try:
        return resolve(conn, company_id, fields)
    except (TypeError, ValueError, AttributeError):
        return resolve(conn, company_id, fields["name"])


def _call_add_relation(relations_mod, conn, company_id, src_name, rel,
                       dst_name):
    """Add one relation by endpoint NAME (the sibling resolves ids).

    Real sibling signature: add_relation(conn, company_id, src_name,
    rel, dst_name, evidence=None) -- note the order is src, rel, dst,
    and endpoints are names, never ids (passing a UUID string as a
    name would create a bogus entity named by that id). evidence=[]
    is passed when accepted.
    """
    add = relations_mod.add_relation
    try:
        names = [p.name for p in inspect.signature(add).parameters.values()]
    except (TypeError, ValueError):
        names = []
    if "evidence" in names:
        return add(conn, company_id, src_name, rel, dst_name, evidence=[])
    try:
        return add(conn, company_id, src_name, rel, dst_name, [])
    except TypeError:
        return add(conn, company_id, src_name, rel, dst_name)


def _normalize_rel(rel) -> str:
    """Mirror the sibling's relation normalisation for existence checks."""
    return " ".join(str(rel).strip().lower().split())


def _resolve_and_count(entities_mod, conn, company_id, fields: dict,
                       entity_ids: dict, seen_entity_ids: set) -> tuple:
    """Resolve one entity; return (entity_id, was_newly_counted).

    Names already resolved in this call reuse the cached id and are
    never re-counted. Newness is exact when the sibling offers
    get_entity: an entity that already exists (by name or alias) is
    not counted; only a resolve that creates a row counts. Without
    get_entity, a bare id with no newness flag counts only on first
    sight within this call, and a sibling-reported is_new flag wins
    over both fallbacks.
    """
    name = fields["name"]
    if name in entity_ids:
        return entity_ids[name], False
    get_entity = getattr(entities_mod, "get_entity", None)
    if callable(get_entity):
        existing = get_entity(conn, company_id, name)
        if existing is not None:
            entity_id = existing.get("id") if isinstance(existing, dict) \
                else _result_id(existing)
            if entity_id is not None:
                entity_ids[name] = entity_id
                if fields.get("display_name"):
                    entity_ids.setdefault(fields["display_name"], entity_id)
                return entity_id, False
        result = _call_resolve_entity(entities_mod, conn, company_id, fields)
        entity_id = _result_id(result)
        if entity_id is not None:
            entity_ids[name] = entity_id
            if fields.get("display_name"):
                entity_ids.setdefault(fields["display_name"], entity_id)
            return entity_id, True
        return None, False
    result = _call_resolve_entity(entities_mod, conn, company_id, fields)
    entity_id = _result_id(result)
    flag = _is_new_result(result)
    if entity_id is not None:
        entity_ids[name] = entity_id
        if fields.get("display_name"):
            entity_ids.setdefault(fields["display_name"], entity_id)
    if flag is True:
        return entity_id, True
    if flag is False:
        return entity_id, False
    if entity_id is not None and entity_id not in seen_entity_ids:
        seen_entity_ids.add(entity_id)
        return entity_id, True
    if entity_id is not None:
        seen_entity_ids.add(entity_id)
    return entity_id, False


def _endpoint_name(endpoint):
    """Reduce a relation endpoint to the entity NAME for add_relation.

    Returns the name string, or None when the endpoint carries no
    name (a bare id, or a dict with only an id): add_relation takes
    names and resolves ids itself, and a UUID string passed as a name
    would create a bogus entity, so id-only endpoints are skipped by
    the caller rather than guessed at.
    """
    if isinstance(endpoint, dict):
        return endpoint.get("name") or endpoint.get("display_name")
    if isinstance(endpoint, str):
        try:
            uuid.UUID(endpoint)
            return None
        except (ValueError, AttributeError, TypeError):
            return endpoint
    name = getattr(endpoint, "name", None) \
        or getattr(endpoint, "display_name", None)
    return name if isinstance(name, str) else None


def _process_chunk(conn, company_id, chunk_text, entities_mod, relations_mod,
                   entity_ids: dict, seen_entity_ids: set,
                   seen_relation_keys: set) -> tuple[int, int]:
    """Extract + resolve entities and relations for one chunk.

    Returns (new_entities, new_relations) counted for this chunk.
    Relation newness is exact: the (company, src, dst, rel) row is
    looked up by the resolved endpoint ids before add_relation runs,
    so an upserted (already-existing) relation is never counted.
    """
    new_entities = 0
    new_relations = 0
    for item in entities_mod.extract_entities(chunk_text) or []:
        fields = _entity_fields(item)
        if not fields["name"]:
            continue
        _entity_id, counted = _resolve_and_count(
            entities_mod, conn, company_id, fields,
            entity_ids, seen_entity_ids)
        new_entities += 1 if counted else 0
    for item in relations_mod.extract_relations(chunk_text) or []:
        src, dst, rel = _relation_fields(item)
        if src is None or dst is None or rel is None:
            continue
        src_name = _endpoint_name(src)
        dst_name = _endpoint_name(dst)
        if not src_name or not dst_name:
            continue
        src_id, src_counted = _resolve_and_count(
            entities_mod, conn, company_id,
            {"name": src_name, "display_name": src_name, "kind": "entity"},
            entity_ids, seen_entity_ids)
        dst_id, dst_counted = _resolve_and_count(
            entities_mod, conn, company_id,
            {"name": dst_name, "display_name": dst_name, "kind": "entity"},
            entity_ids, seen_entity_ids)
        new_entities += (1 if src_counted else 0) + (1 if dst_counted else 0)
        if src_id is None or dst_id is None:
            continue
        rel_norm = _normalize_rel(rel)
        existing = conn.execute(
            "SELECT id FROM engine_relations WHERE company_id = %s "
            "AND src_id = %s AND dst_id = %s AND rel = %s",
            (company_id, src_id, dst_id, rel_norm),
        ).fetchone()
        _call_add_relation(
            relations_mod, conn, company_id, src_name, rel, dst_name)
        if existing is None:
            new_relations += 1
    # Aliases learned from the content itself: "Sick Leave (SL)"
    # teaches the engine that SL is the sick-leave entity. This is the
    # ONLY way an abbreviation comes to resolve — there is no hand
    # list. Alias learning never inflates the new-entity count, and a
    # failure here never fails the upload: learning is additive.
    # Collision fold: the short form may already exist as its own bare
    # entity (an LLM extractor can return "SL" as an entity). A bare
    # short-form node with no relations carries no graph information,
    # so it is folded into the full-name entity — its name becomes the
    # alias. A short form attached to a real, related entity is left
    # alone: one short form never points at two entities.
    extract_aliases = getattr(entities_mod, "extract_aliases", None)
    add_alias = getattr(entities_mod, "add_alias", None)
    merge_entities = getattr(entities_mod, "merge_entities", None)
    get_entity = getattr(entities_mod, "get_entity", None)
    norm_name = getattr(entities_mod, "normalize_name", None)
    if callable(extract_aliases) and callable(add_alias):
        for full_name, short in extract_aliases(chunk_text) or []:
            try:
                target_id, _counted = _resolve_and_count(
                    entities_mod, conn, company_id,
                    {"name": full_name, "display_name": full_name,
                     "kind": "entity"},
                    entity_ids, seen_entity_ids)
                if not target_id:
                    continue
                if add_alias(conn, company_id, target_id, short):
                    continue
                if (
                    callable(merge_entities)
                    and callable(get_entity)
                    and callable(norm_name)
                ):
                    other = get_entity(conn, company_id, short)
                    if (
                        other is not None
                        and str(other.get("id")) != str(target_id)
                        and norm_name(other.get("name") or "") == norm_name(short)
                    ):
                        rel_count = conn.execute(
                            "SELECT COUNT(*) FROM engine_relations "
                            "WHERE company_id = %s "
                            "AND (src_id = %s OR dst_id = %s)",
                            (company_id, other["id"], other["id"]),
                        ).fetchone()[0]
                        if rel_count == 0:
                            merge_entities(
                                conn, company_id, target_id, other["id"]
                            )
                            if str(other["id"]) in {
                                str(v) for v in entity_ids.values()
                            }:
                                new_entities -= 1
            except Exception:
                continue
    return new_entities, new_relations


# ---------------------------------------------------------------------------
# Shared DB write path
# ---------------------------------------------------------------------------

def _insert_document(conn, company_id, custom_id, title, content_hash) -> str:
    """Insert the documents row (content='' by design) and return its id."""
    row = conn.execute(
        "INSERT INTO documents (company_id, custom_id, title, content, "
        "content_hash, status, processing, chunk_count) VALUES "
        "(%s, %s, %s, '', %s, 'done', %s::jsonb, 0) RETURNING id",
        (company_id, custom_id, title, content_hash,
         json.dumps([{"step": "engine-chunked-ingest"}])),
    ).fetchone()
    return str(row[0])


def _write_chunks(conn, company_id, doc_id, title, chunk_iter,
                  entities_mod, relations_mod, batch_commit: int) -> tuple:
    """Write one chunks row per window plus per-chunk graph rows.

    position follows the existing repo convention (store.py): the
    0-based sequential chunk index, ordered by chunk_stream's
    char_offset order. embedded_content is the chunk text itself and
    embedding is the embed_document vector or NULL (never faked).
    Commits every batch_commit chunks; the caller commits at the end.
    Returns (chunk_count, new_entities, new_relations).
    """
    chunk_count = 0
    entities_new = 0
    relations_new = 0
    entity_ids: dict = {}
    seen_entity_ids: set = set()
    seen_relation_keys: set = set()
    for position, (_char_offset, chunk_text) in enumerate(chunk_iter):
        vec = _safe_embed(title, chunk_text)
        conn.execute(
            "INSERT INTO chunks (document_id, company_id, position, "
            "content, embedded_content, embedding) VALUES "
            "(%s, %s, %s, %s, %s, %s)",
            (doc_id, company_id, position, chunk_text, chunk_text, vec),
        )
        ent_n, rel_n = _process_chunk(
            conn, company_id, chunk_text, entities_mod, relations_mod,
            entity_ids, seen_entity_ids, seen_relation_keys)
        entities_new += ent_n
        relations_new += rel_n
        chunk_count += 1
        if chunk_count % batch_commit == 0:
            conn.commit()
    return chunk_count, entities_new, relations_new


def _duplicate_result(content_hash: str) -> dict:
    """Return shape for a duplicate upload: no rows were written."""
    return {
        "duplicate": True,
        "chunks": 0,
        "entities": 0,
        "relations": 0,
        "document_id": None,
        "content_hash": content_hash,
    }


def _new_result(doc_id: str, chunk_count: int, entities_new: int,
                relations_new: int, content_hash: str) -> dict:
    return {
        "duplicate": False,
        "chunks": chunk_count,
        "entities": entities_new,
        "relations": relations_new,
        "document_id": doc_id,
        "content_hash": content_hash,
    }


# ---------------------------------------------------------------------------
# Public ingest entry points
# ---------------------------------------------------------------------------

def ingest_file(conn, company_id, source, filename, title=None,
                batch_commit: int = _DEFAULT_BATCH_COMMIT) -> dict:
    """Ingest a file by path or bytes, streaming, in two passes.

    source is a filesystem path (str/Path) or a bytes/bytearray
    object. A path is opened once per pass in binary mode and never
    read whole; bytes input is hashed directly (the caller already
    holds it -- bytes input is the caller's memory responsibility)
    and chunked from an in-memory stream. Any other source type
    raises TypeError: a one-shot stream cannot be rewound for the
    second pass, and buffering it would break the streaming rule.

    title defaults to filename. Returns {duplicate, chunks, entities,
    relations, document_id, content_hash}; on duplicate, no rows are
    written at all. On any failure during the write pass the
    transaction is rolled back and the error re-raised.
    """
    if not isinstance(batch_commit, int) or isinstance(batch_commit, bool) \
            or batch_commit < 1:
        raise ValueError("batch_commit must be a positive int")
    doc_title = title or filename
    dedup, entities_mod, relations_mod = _engine_helpers()

    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        content_hash = hashlib.sha256(data).hexdigest()
        if not _is_new_register(
                _call_register_upload(
                    dedup, conn, company_id, content_hash, filename)):
            return _duplicate_result(content_hash)
        try:
            doc_id = _insert_document(
                conn, company_id, f"engine:{content_hash}",
                doc_title, content_hash)
            chunk_count, entities_new, relations_new = _write_chunks(
                conn, company_id, doc_id, doc_title,
                chunk_stream(io.BytesIO(data),
                             _DEFAULT_CHUNK_CHARS, _DEFAULT_OVERLAP),
                entities_mod, relations_mod, batch_commit)
            conn.execute(
                "UPDATE documents SET chunk_count = %s, updated_at = now() "
                "WHERE id = %s",
                (chunk_count, doc_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _new_result(doc_id, chunk_count, entities_new,
                           relations_new, content_hash)

    if isinstance(source, (str, Path)):
        path = Path(source)
        # Pass 1: hash + count only, no DB writes.
        with open(path, "rb") as handle:
            content_hash, pass1_count = _deoverlapped_hash_and_count(
                handle, _DEFAULT_CHUNK_CHARS, _DEFAULT_OVERLAP)
        if not _is_new_register(
                _call_register_upload(
                    dedup, conn, company_id, content_hash, filename)):
            return _duplicate_result(content_hash)
        try:
            doc_id = _insert_document(
                conn, company_id, f"engine:{content_hash}",
                doc_title, content_hash)
            # Pass 2: writes only, re-opening the path.
            with open(path, "rb") as handle:
                chunk_count, entities_new, relations_new = _write_chunks(
                    conn, company_id, doc_id, doc_title,
                    chunk_stream(handle,
                                 _DEFAULT_CHUNK_CHARS, _DEFAULT_OVERLAP),
                    entities_mod, relations_mod, batch_commit)
            if chunk_count != pass1_count:
                # The file changed between passes: stored chunks
                # would not match the hash. Roll back, do not store.
                conn.rollback()
                raise RuntimeError(
                    "source changed between ingest passes "
                    f"(pass 1 saw {pass1_count} chunks, pass 2 saw "
                    f"{chunk_count}); nothing was stored"
                )
            conn.execute(
                "UPDATE documents SET chunk_count = %s, updated_at = now() "
                "WHERE id = %s",
                (chunk_count, doc_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _new_result(doc_id, chunk_count, entities_new,
                           relations_new, content_hash)

    raise TypeError(
        "ingest_file source must be a filesystem path (str/Path) or "
        f"bytes, got {type(source).__name__}; one-shot streams are not "
        "accepted because the two-pass design needs a re-readable source"
    )


def ingest_text(conn, company_id, text, filename, title=None) -> dict:
    """Ingest small in-memory text (tests, chat-sized input).

    Hashes first and dedup-checks before writing anything, so no
    second pass is needed. Chunking still goes through chunk_stream
    over an in-memory stream, and chunks/entities/relations are
    written exactly as in ingest_file. Same return shape.
    """
    doc_title = title or filename
    dedup, entities_mod, relations_mod = _engine_helpers()
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if not _is_new_register(
            _call_register_upload(
                dedup, conn, company_id, content_hash, filename)):
        return _duplicate_result(content_hash)
    try:
        doc_id = _insert_document(
            conn, company_id, f"engine:{content_hash}",
            doc_title, content_hash)
        chunk_count, entities_new, relations_new = _write_chunks(
            conn, company_id, doc_id, doc_title,
            chunk_stream(io.StringIO(text),
                         _DEFAULT_CHUNK_CHARS, _DEFAULT_OVERLAP),
            entities_mod, relations_mod, _DEFAULT_BATCH_COMMIT)
        conn.execute(
            "UPDATE documents SET chunk_count = %s, updated_at = now() "
            "WHERE id = %s",
            (chunk_count, doc_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _new_result(doc_id, chunk_count, entities_new,
                       relations_new, content_hash)
