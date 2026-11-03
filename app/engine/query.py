"""Graph question answering: entity paths with role-filtered relations.

Additive module: it never modifies existing modules and is never wired
into them here. Callers use ``answer_question_engine_or_legacy`` as the
thin wiring guarantee — on any engine failure or empty engine result,
the existing ``app.answer`` path answers instead.

Role filtering: engine_relations rows are loaded with the SAME
whitelisted role literals as ``app.retrieve`` (see ``_ROLE_PREDICATES``).
The role string is never interpolated into SQL; only a whitelisted
literal is. Traversal data loading and the path search live here, in
Python over those role-filtered rows, because the sibling
``app.engine.relations`` traversal signatures carry no role parameter —
delegating to them would bypass the role filter.

Paths contain only real entity names and relation types from the
database. Nothing here invents facts, entities, or relations, and no
provider, model, or key is hardcoded: LLM synthesis goes only through
``app.llm``. Importing this module needs no database connection and no
LLM key; missing data yields an empty result, never an exception.
"""

from app import retrieve

try:  # app.llm is built in parallel; absence -> rendered-path answer.
    from app import llm
except ImportError:  # pragma: no cover - parallel-build tolerance
    llm = None

try:  # Existing legacy path, used only by the fallback wrapper.
    from app import answer as _legacy_answer
except ImportError:  # pragma: no cover - parallel-build tolerance
    _legacy_answer = None

try:  # Sibling module, built in parallel; absence -> empty result.
    from app.engine.entities import extract_entities as _extract_entities
except Exception:  # pragma: no cover - parallel-build tolerance
    _extract_entities = None

# Hierarchy role predicates, mirroring app.retrieve exactly: CEO sees
# everything; CFO sees finance + general; CTO sees technical + general;
# employee (and any unknown role, least privilege) sees general only.
# 'manager' is retrieve's legacy role string, kept working. Predicates
# use whitelisted literals only — the role string is never interpolated.
_ROLE_PREDICATES = {
    "ceo": "TRUE",
    "cfo": "(visible_to @> ARRAY['cfo'] OR visible_to @> ARRAY['employee'])",
    "cto": "(visible_to @> ARRAY['cto'] OR visible_to @> ARRAY['employee'])",
    "employee": "visible_to @> ARRAY['employee']",
    "manager": "(visible_to @> ARRAY['manager'] OR visible_to @> ARRAY['employee'])",
}

_MAX_HOPS = 4


def _role_predicate(role: str) -> str:
    return _ROLE_PREDICATES.get(role, _ROLE_PREDICATES["employee"])


def _empty_result() -> dict:
    return {
        "answer": "",
        "path": [],
        "facts": [],
        "citations": [],
        "empty": True,
        "source": "engine",
    }


def _normalize_name(name: str) -> str:
    """Lowercased, whitespace-collapsed name; underscores count as spaces."""
    return " ".join(str(name).replace("_", " ").strip().lower().split())


def _row_value(row, index: int, key: str):
    """Read one column from a tuple row or a dict row (RealDictCursor)."""
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[index]
    except (IndexError, TypeError):
        return None


def _render_path(path) -> str:
    """Render a path's steps as a chain, e.g. 'A —manages→ B —approves→ C'.

    ``path`` is a list of step dicts ({from, relationship, to, reversed})
    or a dict wrapping such a list under 'path'. A reversed step is an
    edge traversed against its stored direction, so its arrow points
    backwards ('A ←manages— B'). Steps with no usable names are skipped;
    an empty or malformed path renders as ''. Only real stored names
    and relation types ever appear in the output.
    """
    steps = path
    if isinstance(path, dict):
        if "path" in path:
            steps = path["path"]
        elif "from" in path or "to" in path:
            steps = [path]
        else:
            return ""
    if not isinstance(steps, (list, tuple)) or not steps:
        return ""
    rendered_steps = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        src = step.get("from", step.get("src", step.get("source")))
        dst = step.get("to", step.get("dst", step.get("destination")))
        rel = step.get(
            "relationship", step.get("rel", step.get("relation"))
        )
        if not isinstance(src, str) or not src.strip():
            continue
        if not isinstance(dst, str) or not dst.strip():
            continue
        if not isinstance(rel, str) or not rel.strip():
            continue
        src = src.strip()
        dst = dst.strip()
        rel = rel.strip()
        if step.get("reversed"):
            rendered_steps.append((src, " ←" + rel + "— ", dst))
        else:
            rendered_steps.append((src, " —" + rel + "→ ", dst))
    if not rendered_steps:
        return ""
    # Chain-join: the shared intermediate node is printed once, so
    # 'A —manages→ B' + 'B —approves→ C' becomes 'A —manages→ B —approves→ C'.
    out = rendered_steps[0][0]
    for _src, arrow, dst in rendered_steps:
        out += arrow + dst
    return out


def _load_entities(conn, company_id):
    """entity id -> (display name, [normalized lookup keys]).

    Lookup keys cover name, display_name, and every alias, so a question
    entity resolves to the same node however it is named in the question.
    """
    entities = {}
    lookups = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, display_name, aliases FROM engine_entities "
            "WHERE company_id = %s ORDER BY name",
            (company_id,),
        )
        for row in cur.fetchall():
            eid = _row_value(row, 0, "id")
            name = _row_value(row, 1, "name")
            display = _row_value(row, 2, "display_name")
            aliases = _row_value(row, 3, "aliases")
            if eid is None:
                continue
            shown = display.strip() if isinstance(display, str) and display.strip() else None
            if shown is None and isinstance(name, str) and name.strip():
                shown = name.strip()
            if shown is None:
                continue
            entities[eid] = shown
            keys = set()
            for value in (name, display):
                if isinstance(value, str) and value.strip():
                    keys.add(_normalize_name(value))
            if isinstance(aliases, (list, tuple)):
                for alias in aliases:
                    if isinstance(alias, str) and alias.strip():
                        keys.add(_normalize_name(alias))
            elif isinstance(aliases, str) and aliases.strip():
                keys.add(_normalize_name(aliases))
            for key in keys:
                if key:
                    lookups.setdefault(key, eid)
    return entities, lookups


def _load_role_filtered_edges(conn, company_id, role):
    """Role-filtered relations, joined to their endpoint entities.

    Only engine_relations carries visible_to, so the bare whitelisted
    predicate resolves to that table's column. Endpoint entities are
    restricted to the same company. Returns rows of
    (src_id, dst_id, rel, weight, src_display, dst_display, evidence).
    """
    predicate = _role_predicate(role)
    edges = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.src_id, r.dst_id, r.rel, r.weight, "
            "s.name, s.display_name, d.name, d.display_name, r.evidence "
            "FROM engine_relations r "
            "JOIN engine_entities s ON s.id = r.src_id "
            "JOIN engine_entities d ON d.id = r.dst_id "
            f"WHERE r.company_id = %s AND {predicate} "
            "AND s.company_id = %s AND d.company_id = %s",
            (company_id, company_id, company_id),
        )
        for row in cur.fetchall():
            src_id = _row_value(row, 0, "src_id")
            dst_id = _row_value(row, 1, "dst_id")
            rel = _row_value(row, 2, "rel")
            weight = _row_value(row, 3, "weight")
            src_name = _row_value(row, 4, "name")
            src_display = _row_value(row, 5, "display_name")
            dst_name = _row_value(row, 6, "name")
            dst_display = _row_value(row, 7, "display_name")
            evidence = _row_value(row, 8, "evidence")
            if src_id is None or dst_id is None:
                continue
            if not isinstance(rel, str) or not rel.strip():
                continue
            try:
                score = float(weight) if weight is not None else 1.0
            except (TypeError, ValueError):
                score = 1.0
            src_shown = (
                src_display.strip()
                if isinstance(src_display, str) and src_display.strip()
                else src_name.strip()
                if isinstance(src_name, str) and src_name.strip()
                else None
            )
            dst_shown = (
                dst_display.strip()
                if isinstance(dst_display, str) and dst_display.strip()
                else dst_name.strip()
                if isinstance(dst_name, str) and dst_name.strip()
                else None
            )
            if src_shown is None or dst_shown is None:
                continue
            edges.append((src_id, dst_id, rel.strip(), score, src_shown, dst_shown, evidence))
    return edges


def _build_graph(conn, company_id, role):
    """Load the role-filtered graph: display names + adjacency.

    Adjacency entries are (neighbor_id, relationship, weight, reversed, evidence):
    every stored edge is walkable in both directions, with the reverse
    direction flagged so rendering keeps the true edge direction.
    """
    entities, lookups = _load_entities(conn, company_id)
    edges = _load_role_filtered_edges(conn, company_id, role)
    adjacency = {}
    for src_id, dst_id, rel, score, src_shown, dst_shown, evidence in edges:
        entities.setdefault(src_id, src_shown)
        entities.setdefault(dst_id, dst_shown)
        adjacency.setdefault(src_id, []).append((dst_id, rel, score, False, evidence))
        adjacency.setdefault(dst_id, []).append((src_id, rel, score, True, evidence))
    for neighbours in adjacency.values():
        neighbours.sort(key=lambda item: (entities.get(item[0], ""), item[1]))
    return entities, lookups, adjacency


def _enumerate_paths(entities, adjacency, start_id, target_id, max_hops):
    """All simple paths from start_id, up to max_hops hops.

    When target_id is given, only paths ending at it are returned;
    otherwise every non-empty path is returned. Each result is
    {'path': [step dicts], 'score': summed edge weights}.
    """
    results = []
    stack = [(start_id, [], 0.0, {start_id})]
    while stack:
        current, path, score, visited = stack.pop()
        if len(path) >= max_hops:
            continue
        for neighbour, rel, weight, reversed_flag, evidence in adjacency.get(current, []):
            if neighbour in visited:
                continue
            step = {
                "from": entities.get(current, ""),
                "relationship": rel,
                "to": entities.get(neighbour, ""),
                "reversed": reversed_flag,
                "evidence": evidence,
            }
            if not step["from"] or not step["to"]:
                continue
            new_path = path + [step]
            new_score = score + weight
            if target_id is None or neighbour == target_id:
                results.append({"path": new_path, "score": new_score})
            if len(new_path) < max_hops:
                stack.append(
                    (neighbour, new_path, new_score, visited | {neighbour})
                )
    return results


def _find_path(conn, company_id, role, start_name, end_name, max_hops=_MAX_HOPS):
    """Role-filtered shortest-then-best path between two entity names.

    Returns {'path': [steps], 'score': float} or None when either name
    does not resolve, both names resolve to the same node, or no path
    of at most max_hops hops exists. Shortest wins; ties break on score,
    then on the rendered chain (deterministic).
    """
    entities, lookups, adjacency = _build_graph(conn, company_id, role)
    start_id = lookups.get(_normalize_name(start_name))
    end_id = lookups.get(_normalize_name(end_name))
    if start_id is None or end_id is None or start_id == end_id:
        return None
    candidates = _enumerate_paths(
        entities, adjacency, start_id, end_id, max_hops
    )
    if not candidates:
        return None
    candidates.sort(
        key=lambda c: (len(c["path"]), -c["score"], _render_path(c["path"]))
    )
    return candidates[0]


def _traverse(conn, company_id, role, start_name, max_hops=_MAX_HOPS):
    """Role-filtered paths out of one entity, best-scoring first.

    Returns [{'path': [steps], 'score': float}, ...], sorted by score
    descending, then hops ascending, then rendered chain. Empty when the
    name does not resolve or the node has no role-visible edges.
    """
    entities, lookups, adjacency = _build_graph(conn, company_id, role)
    start_id = lookups.get(_normalize_name(start_name))
    if start_id is None:
        return []
    candidates = _enumerate_paths(entities, adjacency, start_id, None, max_hops)
    candidates.sort(
        key=lambda c: (-c["score"], len(c["path"]), _render_path(c["path"]))
    )
    return candidates


def _build_citations(facts, conn=None):
    """One citation per fact, mirroring app.answer's shape and order.

    Document ids first (titles looked up when a connection is available),
    else the chat message id, else the source itself. Deduped on
    (memory_id, source, source_ref). Never raises.
    """
    doc_titles = {}
    doc_ids_all = set()
    for fact in facts or []:
        if isinstance(fact, dict):
            for doc_id in fact.get("document_ids") or []:
                doc_ids_all.add(str(doc_id))
    if doc_ids_all and conn is not None and hasattr(conn, "cursor"):
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, title FROM documents WHERE id = ANY(%s::uuid[])",
                    (list(doc_ids_all),),
                )
                for did, title in cur.fetchall():
                    if title:
                        doc_titles[str(did)] = title
        except Exception:
            doc_titles = {}
    citations = []
    seen = set()
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        memory_id = fact.get("id")
        source = fact.get("source")
        doc_ids = fact.get("document_ids") or []
        if doc_ids:
            refs = [doc_titles.get(str(d), str(d)) for d in doc_ids]
        elif fact.get("source_message_id"):
            refs = [fact["source_message_id"]]
        else:
            refs = [source]
        for ref in refs:
            key = (memory_id, source, ref)
            if key not in seen:
                seen.add(key)
                citations.append(
                    {
                        "memory_id": memory_id,
                        "source": source,
                        "source_ref": ref,
                    }
                )
    return citations


def _link_entities(conn, company_id, question) -> list:
    """Link known entities mentioned in the question, in order.

    Matches display names and aliases as case-insensitive substrings of
    the question, longest match wins on overlap, order = first mention.
    This is entity linking against real stored rows only — nothing is
    invented. Returns [] when nothing known is mentioned.
    """
    text = (question or "").lower()
    if not text.strip():
        return []
    candidates = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT display_name, aliases, name FROM engine_entities "
            "WHERE company_id = %s",
            (company_id,),
        )
        for display, aliases, name in cur.fetchall():
            shown = None
            if isinstance(display, str) and display.strip():
                shown = display.strip()
            elif isinstance(name, str) and name.strip():
                shown = name.strip()
            if not shown:
                continue
            forms = {shown, shown.replace("_", " ")}
            if isinstance(aliases, (list, tuple)):
                for alias in aliases:
                    if isinstance(alias, str) and alias.strip():
                        forms.add(alias.strip())
                        forms.add(alias.strip().replace("_", " "))
            elif isinstance(aliases, str) and aliases.strip():
                forms.add(aliases.strip())
                forms.add(aliases.strip().replace("_", " "))
            for form in forms:
                pos = text.find(form.lower())
                if pos >= 0:
                    candidates.append((pos, -len(form), shown, form))
    candidates.sort()
    kept = []
    for pos, _neglen, shown, form in candidates:
        end = pos + len(form)
        if any(not (end <= s or pos >= e) for s, e, _ in kept):
            continue
        kept.append((pos, end, shown))
    kept.sort()
    out = []
    seen = set()
    for _pos, _end, shown in kept:
        if shown not in seen:
            seen.add(shown)
            out.append(shown)
    return out


def answer_with_graph(conn, company_id, role, question, alias_map=None) -> dict:
    """Answer a question from a role-filtered entity path.

    (1) Link known entities mentioned in the question (text extraction
    as fallback); none -> empty result.
    (2) 2+ entities: path from the first to the last (max 4 hops);
    exactly 1 entity: best-scoring path out of it (max 4 hops). No path
    -> empty result. (3) With an LLM key, synthesize from retrieved
    facts; on a non-empty synthesis the answer is the synthesis with the
    search facts and citations. Otherwise the answer is the rendered
    path, with any retrieved search facts included and no citations.
    Never raises for missing data — returns an empty result instead.
    """
    try:
        names = []
        try:
            names = _link_entities(conn, company_id, question)
        except Exception:
            names = []
        if not names:
            try:
                from app.engine.entities import _extract_entities_deterministic
                raw_entities = _extract_entities_deterministic(question)
            except Exception:
                return _empty_result()
            if not raw_entities:
                return _empty_result()
            seen_names = set()
            for entry in raw_entities:
                name = None
                if isinstance(entry, dict):
                    name = entry.get("name")
                elif isinstance(entry, str):
                    name = entry
                if isinstance(name, str) and name.strip():
                    key = _normalize_name(name)
                    if key and key not in seen_names:
                        seen_names.add(key)
                        names.append(name.strip())
        if not names:
            return _empty_result()
        try:
            if len(names) >= 2:
                found = _find_path(
                    conn, company_id, role, names[0], names[-1],
                    max_hops=_MAX_HOPS,
                )
            else:
                paths = _traverse(
                    conn, company_id, role, names[0], max_hops=_MAX_HOPS
                )
                found = paths[0] if paths else None
        except Exception:
            return _empty_result()
        if not found or not found.get("path"):
            return _empty_result()
        path = found["path"]

        facts = []
        evidence_mids = []
        for step in path:
            ev = step.get("evidence")
            if isinstance(ev, list):
                evidence_mids.extend([str(m) for m in ev if m])
        if evidence_mids:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT m.id, m.fact, m.version, m.is_latest, m.status, m.effective_date, m.visible_to, m.source, "
                        "array_agg(ms.document_id) FILTER (WHERE ms.document_id IS NOT NULL) AS doc_ids "
                        "FROM memories m "
                        "LEFT JOIN memory_sources ms ON ms.memory_id = m.id "
                        "WHERE m.id = ANY(%s::uuid[]) AND m.is_latest = true AND m.status = 'current' "
                        "GROUP BY m.id",
                        (evidence_mids,),
                    )
                    for mid, fact_text, ver, is_lat, stat, eff, vis, src, d_ids in cur.fetchall():
                        facts.append({
                            "id": str(mid),
                            "fact": fact_text,
                            "version": ver,
                            "is_latest": is_lat,
                            "status": stat,
                            "effective_date": eff.isoformat() if eff else None,
                            "visible_to": vis,
                            "source": src,
                            "document_ids": [str(d) for d in (d_ids or [])],
                        })
            except Exception:
                facts = []

        if llm is not None:
            try:
                has_key = llm.has_key()
            except Exception:
                has_key = False
            if has_key:
                if not facts:
                    try:
                        facts = retrieve.search(
                            conn, company_id, role, question, alias_map=alias_map
                        )
                    except Exception:
                        facts = []
                    facts = retrieve.filter_covering(facts, question, alias_map)
                if facts:
                    try:
                        synthesis = llm.synthesize(question, facts, [])
                    except Exception:
                        synthesis = ""
                    if synthesis and str(synthesis).strip():
                        return {
                            "answer": synthesis,
                            "path": path,
                            "facts": facts,
                            "citations": _build_citations(facts, conn),
                            "empty": False,
                            "source": "engine",
                        }
        rendered = _render_path(path)
        return {
            "answer": rendered,
            "path": path,
            "facts": facts,
            "citations": [],
            "empty": rendered == "",
            "source": "engine",
        }
    except Exception:
        return _empty_result()


def answer_question_engine_or_legacy(
    conn, company_id, role, question, alias_map=None
) -> dict:
    """Engine first, legacy ``app.answer`` on any engine failure or empty.

    Engine success returns the engine result marked engine_fallback=False;
    any exception from the engine, or an empty engine result, falls back
    to ``app.answer.answer_question`` marked engine_fallback=True. The
    engine therefore never breaks the existing path. This function is
    provided only — it is NOT wired into whatsapp.py or any existing
    module here.
    """
    result = None
    try:
        result = answer_with_graph(
            conn, company_id, role, question, alias_map=alias_map
        )
    except Exception:
        result = None
    if result is not None and not result.get("empty"):
        out = dict(result)
        out["engine_fallback"] = False
        return out
    if _legacy_answer is not None:
        legacy = _legacy_answer.answer_question(
            conn, company_id, role, question, alias_map=alias_map
        )
        out = dict(legacy)
        out["engine_fallback"] = True
        return out
    out = dict(result) if result is not None else _empty_result()
    out["engine_fallback"] = True
    return out
