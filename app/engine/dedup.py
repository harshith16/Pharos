"""Upload dedup + entity alias-collision merging (own code only).

Tables (created by a separate migration; this module never creates
schema):
- engine_uploads(id, company_id, content_hash, filename, created_at)
  with UNIQUE(company_id, content_hash).
- engine_entities(id, company_id, name, display_name, kind, aliases,
  embedding, created_at, updated_at) with UNIQUE(company_id, name).
- engine_relations: related entity pairs. Its endpoint columns are
  resolved at runtime (see _relation_columns) instead of assumed, so
  this module does not import app.engine.entities (import-cycle
  avoidance) and does not hardcode a relations schema it does not own.

No function here calls conn.commit(): callers own the transaction.
Hashes are deterministic SHA-256 only: no randomness, no timestamps
in any hash.
"""

import hashlib
import json
import re

try:
    from psycopg.errors import UniqueViolation
except Exception:  # pragma: no cover - psycopg always present in prod
    class UniqueViolation(Exception):
        """Fallback so unique-violation detection still works by pgcode."""


def content_hash(data: bytes) -> str:
    """SHA-256 hex of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def content_hash_text(text: str) -> str:
    """SHA-256 hex of the UTF-8 encoding of text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def register_upload(
    conn, company_id, content_hash, filename
) -> tuple[bool, str]:
    """Record an upload, or return the existing row on hash conflict.

    Returns (True, new_id) on insert, (False, existing_id) when a row
    for (company_id, content_hash) already exists. Never inserts twice
    and never commits. The insert-then-select retry covers the race
    where a concurrent transaction aborts between the two statements.
    """
    for _ in range(2):
        row = conn.execute(
            "INSERT INTO engine_uploads (company_id, content_hash, "
            "filename) VALUES (%s, %s, %s) ON CONFLICT "
            "(company_id, content_hash) DO NOTHING RETURNING id",
            (company_id, content_hash, filename),
        ).fetchone()
        if row is not None:
            return True, str(row[0])
        row = conn.execute(
            "SELECT id FROM engine_uploads WHERE company_id = %s "
            "AND content_hash = %s",
            (company_id, content_hash),
        ).fetchone()
        if row is not None:
            return False, str(row[0])
    raise RuntimeError("register_upload: insert produced no row")


def is_duplicate_file(conn, company_id, content_hash) -> bool:
    """True iff an engine_uploads row exists for company + hash."""
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM engine_uploads WHERE "
        "company_id = %s AND content_hash = %s)",
        (company_id, content_hash),
    ).fetchone()
    return bool(row[0]) if row is not None else False


def _as_alias_list(value) -> list[str]:
    """Normalize an aliases JSONB value to a list of strings."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [k for k in value.keys() if isinstance(k, str)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return [value]
        return _as_alias_list(parsed)
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def _merge_alias_lists(*lists: list[str], exclude: str | None = None) -> list[str]:
    """Deduped union preserving first-seen order, minus `exclude`."""
    merged: list[str] = []
    seen: set[str] = set()
    for aliases in lists:
        for alias in aliases:
            if alias == exclude or alias in seen:
                continue
            seen.add(alias)
            merged.append(alias)
    return merged


def _is_unique_violation(exc: Exception) -> bool:
    """True for a Postgres unique-key violation (pgcode 23505)."""
    return isinstance(exc, UniqueViolation) or getattr(exc, "pgcode", None) == "23505"


_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

_SRC_CANDIDATES = (
    "src",
    "src_id",
    "src_entity_id",
    "source",
    "source_id",
    "source_entity_id",
    "from_id",
    "from_entity_id",
    "subject",
    "subject_id",
)
_DST_CANDIDATES = (
    "dst",
    "dst_id",
    "dst_entity_id",
    "destination",
    "destination_id",
    "destination_entity_id",
    "to_id",
    "to_entity_id",
    "object",
    "object_id",
)


def _relation_columns(conn):
    """Resolve engine_relations endpoint columns, or None to skip.

    Returns (src_col, dst_col, company_col_or_None, src_is_uuid,
    dst_is_uuid). Returns None when the table is absent or no
    recognisable src/dst column pair exists; callers then skip the
    repoint step rather than failing the whole merge on a schema
    they do not own. Column names come from information_schema and
    are matched against a fixed candidate list, then quoted.
    """
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'engine_relations'"
    ).fetchall()
    if not rows:
        return None
    columns = {name: dtype for name, dtype in rows}
    src = next((c for c in _SRC_CANDIDATES if c in columns), None)
    dst = next((c for c in _DST_CANDIDATES if c in columns), None)
    if src is None or dst is None or src == dst:
        return None
    if not _IDENTIFIER.match(src) or not _IDENTIFIER.match(dst):
        return None
    company = "company_id" if "company_id" in columns else None
    return (
        f'"{src}"',
        f'"{dst}"',
        f'"{company}"' if company else None,
        columns[src] == "uuid",
        columns[dst] == "uuid",
    )


def _endpoint_value(is_uuid: bool, entity_id, entity_name):
    """Endpoint value for one side: entity id for uuid columns, else name.

    Relations keyed by id repoint by id; relations keyed by name
    repoint by canonical name. Returns None for an unrecognised
    column type, meaning that side cannot be repointed.
    """
    if is_uuid:
        return entity_id
    return entity_name


def _repoint_relations(
    conn, company_id, survivor, loser, rel_columns
) -> None:
    """Repoint loser relations at the survivor; drop unique violators.

    For each relation touching the loser, the repointed row is written;
    when that write would violate a unique key on engine_relations,
    the loser relation is deleted instead (the survivor already has
    an equivalent relation). In a caller transaction this uses one
    savepoint per relation so a violation never aborts the merge; in
    autocommit mode the duplicate is detected up front instead, since
    savepoints are meaningless there. Never commits.
    """
    if rel_columns is None:
        return
    src_col, dst_col, company_col, src_is_uuid, dst_is_uuid = rel_columns
    loser_src = _endpoint_value(src_is_uuid, loser["id"], loser["name"])
    loser_dst = _endpoint_value(dst_is_uuid, loser["id"], loser["name"])
    survivor_src = _endpoint_value(
        src_is_uuid, survivor["id"], survivor["name"]
    )
    survivor_dst = _endpoint_value(
        dst_is_uuid, survivor["id"], survivor["name"]
    )
    if loser_src is None and loser_dst is None:
        return
    params: list = []
    conditions: list[str] = []
    if loser_src is not None:
        conditions.append(f"{src_col} = %s")
        params.append(loser_src)
    if loser_dst is not None:
        conditions.append(f"{dst_col} = %s")
        params.append(loser_dst)
    where = " OR ".join(conditions)
    if company_col is not None:
        where = f"({where}) AND {company_col} = %s"
        params.append(company_id)
    rows = conn.execute(
        f"SELECT id, {src_col}, {dst_col} FROM engine_relations "
        f"WHERE {where} ORDER BY id",
        tuple(params),
    ).fetchall()
    autocommit = bool(getattr(conn, "autocommit", False))
    for rel_id, old_src, old_dst in rows:
        new_src = (
            survivor_src
            if loser_src is not None and str(old_src) == str(loser_src)
            else old_src
        )
        new_dst = (
            survivor_dst
            if loser_dst is not None and str(old_dst) == str(loser_dst)
            else old_dst
        )
        if str(new_src) == str(old_src) and str(new_dst) == str(old_dst):
            continue
        if autocommit:
            dup_params: list = [new_src, new_dst, rel_id]
            dup_where = f"{src_col} = %s AND {dst_col} = %s AND id <> %s"
            if company_col is not None:
                dup_where += f" AND {company_col} = %s"
                dup_params.append(company_id)
            dup = conn.execute(
                f"SELECT 1 FROM engine_relations WHERE {dup_where} LIMIT 1",
                tuple(dup_params),
            ).fetchone()
            if dup is not None:
                conn.execute(
                    "DELETE FROM engine_relations WHERE id = %s", (rel_id,)
                )
                continue
            conn.execute(
                f"UPDATE engine_relations SET {src_col} = %s, "
                f"{dst_col} = %s WHERE id = %s",
                (new_src, new_dst, rel_id),
            )
            continue
        conn.execute("SAVEPOINT dedup_merge_sp")
        try:
            conn.execute(
                f"UPDATE engine_relations SET {src_col} = %s, "
                f"{dst_col} = %s WHERE id = %s",
                (new_src, new_dst, rel_id),
            )
        except Exception as exc:
            if not _is_unique_violation(exc):
                conn.execute("ROLLBACK TO SAVEPOINT dedup_merge_sp")
                conn.execute("RELEASE SAVEPOINT dedup_merge_sp")
                raise
            conn.execute("ROLLBACK TO SAVEPOINT dedup_merge_sp")
            conn.execute(
                "DELETE FROM engine_relations WHERE id = %s", (rel_id,)
            )
        conn.execute("RELEASE SAVEPOINT dedup_merge_sp")


def merge_alias_collisions(conn, company_id) -> int:
    """Merge same-company entities whose names/aliases collide.

    A pair collides when one's canonical name appears in the other's
    aliases, or their alias lists overlap. Each collision group merges
    into the member whose canonical name is referenced by another
    member's aliases when exactly one such member exists (the entity
    an alias points at is the real one), else into its earliest-created
    member (created_at, then id as a deterministic tiebreak): the
    survivor gains the loser's aliases plus the loser's canonical name,
    deduped and minus its own name; engine_relations endpoints are
    repointed at the survivor (a repoint that would violate a unique
    key deletes that relation instead); then the loser row is deleted.
    Returns the number of entities merged away. Never commits.
    """
    rows = conn.execute(
        "SELECT id, name, aliases, created_at FROM engine_entities "
        "WHERE company_id = %s ORDER BY created_at ASC NULLS LAST, id ASC",
        (company_id,),
    ).fetchall()
    entities = [
        {
            "id": row[0],
            "name": row[1],
            "aliases": _as_alias_list(row[2]),
        }
        for row in rows
    ]
    parent = list(range(len(entities)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(len(entities)):
        aliases_i = set(entities[i]["aliases"])
        for j in range(i + 1, len(entities)):
            aliases_j = set(entities[j]["aliases"])
            if (
                entities[i]["name"] in aliases_j
                or entities[j]["name"] in aliases_i
                or bool(aliases_i & aliases_j)
            ):
                union(i, j)
    groups: dict[int, list[int]] = {}
    for i in range(len(entities)):
        groups.setdefault(find(i), []).append(i)
    rel_columns = _relation_columns(conn)
    merged = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        survivor_index = members[0]
        referenced = []
        for m in members:
            other_aliases = set()
            for other in members:
                if other != m:
                    other_aliases.update(entities[other]["aliases"])
            if entities[m]["name"] in other_aliases:
                referenced.append(m)
        if len(referenced) == 1:
            survivor_index = referenced[0]
        survivor = entities[survivor_index]
        for loser in (entities[m] for m in members if m != survivor_index):
            survivor["aliases"] = _merge_alias_lists(
                survivor["aliases"],
                loser["aliases"],
                [loser["name"]],
                exclude=survivor["name"],
            )
            conn.execute(
                "UPDATE engine_entities SET aliases = %s::jsonb, "
                "updated_at = now() WHERE id = %s AND company_id = %s",
                (json.dumps(survivor["aliases"]), survivor["id"], company_id),
            )
            _repoint_relations(conn, company_id, survivor, loser, rel_columns)
            conn.execute(
                "DELETE FROM engine_entities WHERE id = %s AND company_id = %s",
                (loser["id"], company_id),
            )
            merged += 1
    return merged
