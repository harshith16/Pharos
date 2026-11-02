"""Retrieval: keyword + semantic + entity 1-hop candidates, ranked in Python.

Imports only app.db and app.embed. The caller passes an open connection
(conn); this module never opens one itself, so the db import below is only
kept for the module contract and is tolerated as absent while app.db is
still being built in parallel.

Visibility and company scope are Postgres predicates, never role text
inside embedded content. Scores are INTERNAL ONLY: they are returned in
the search dicts for ranking/debugging and must never be formatted into
user-facing text by any other module.
"""

import re
from datetime import date

from app import embed

try:  # app.db is built in parallel; conn is always passed in, so absence is fine.
    from app.db import get_conn  # noqa: F401  (contract import, unused here)
except ImportError:  # pragma: no cover - parallel-build tolerance
    get_conn = None

STOPWORDS = {
    "the",
    "and",
    "for",
    "what",
    "how",
    "our",
    "policy",
    "policies",
    "is",
    "are",
    "was",
    "were",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "would",
    "a",
    "an",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "with",
    "about",
    "me",
    "my",
    "we",
    "you",
    "your",
    "it",
    "its",
    "this",
    "that",
    "there",
    "here",
    "when",
    "where",
    "who",
    "why",
    "please",
    "tell",
    "give",
    "show",
    "list",
    "per",
    "from",
    "as",
}

ENTITY_BOOST = 0.25  # legacy flat value; hub-damped boost below replaces it
RECENCY_BOOST = 0.1
SUPERSEDED_PENALTY = 0.5
SEMANTIC_LIMIT = 40
SEMANTIC_THRESHOLD = 0.1  # mem0 rule: semantic signal below this is gated first
ENTITY_SIM_MIN = 0.5  # entity matches below this similarity do not count
_TOKEN_RE = re.compile(r"[a-z0-9]+")


# Hierarchy role predicates (founder spec, 29 Oct 2026): anyone may ask
# anything; answers are gated by role. CEO sees everything; CFO sees
# finance + general; CTO sees technical + general; employee sees
# general only. General facts carry 'employee' in visible_to, so the
# CFO/CTO predicates also match the general token. 'manager' is the
# legacy role string, kept working. Unknown roles get the employee
# predicate (least privilege). Predicates use whitelisted literals
# only — the role string is never interpolated from input.
_ROLE_PREDICATES = {
    "ceo": "TRUE",
    "cfo": "(visible_to @> ARRAY['cfo'] OR visible_to @> ARRAY['employee'])",
    "cto": "(visible_to @> ARRAY['cto'] OR visible_to @> ARRAY['employee'])",
    "employee": "visible_to @> ARRAY['employee']",
    "manager": "(visible_to @> ARRAY['manager'] OR visible_to @> ARRAY['employee'])",
}


def _role_predicate(role: str) -> str:
    return _ROLE_PREDICATES.get(role, _ROLE_PREDICATES["employee"])


def _base_filter(include_superseded: bool, role: str) -> str:
    """Shared candidate filter. Company is a bound param; the role
    predicate is a whitelisted literal (see _ROLE_PREDICATES)."""
    latest = "" if include_superseded else "AND is_latest = true"
    return (
        "company_id = %s "
        f"AND {_role_predicate(role)} "
        "AND is_forgotten = false "
        f"{latest} "
        "AND (forget_after IS NULL OR forget_after > now())"
    )


def _tokens(query: str) -> list:
    # 2-letter words are kept: leave-policy abbreviations (SL, CL, EL,
    # PL) carry the whole meaning, and dropping them was the "Rule for
    # sl" failure (only "rule" was ever searched).
    return [
        t
        for t in _TOKEN_RE.findall(query.lower())
        if len(t) >= 2 and t not in STOPWORDS
    ]


# Abbreviation expansion for retrieval (mem0-style query handling:
# expand the query, never rewrite stored facts). Both the abbreviation
# and the expanded form are searched.
ABBREVIATIONS = {
    "sl": ("sick", "leave"),
    "cl": ("casual", "leave"),
    "el": ("earned", "leave"),
    "pl": ("privilege", "leave"),
}

# Generic words that never count as coverage keys: they appear in
# almost every policy question and would let any fact "cover" any
# question (the frequency-rule failure: "rule" alone matched).
_GENERIC_KEYS = {"rule", "rules"}


def expand_tokens(tokens: list) -> list:
    """Tokens plus abbreviation expansions, order-preserving, deduped."""
    out = list(tokens)
    for tok in tokens:
        for exp in ABBREVIATIONS.get(tok, ()):
            if exp not in out:
                out.append(exp)
    return out


def key_tokens(query: str) -> list:
    """Coverage keys for a question: expanded tokens minus generics."""
    return [t for t in expand_tokens(_tokens(query)) if t not in _GENERIC_KEYS]


def _entity_keys(tokens: list) -> set:
    """Whole-name match keys: single tokens plus adjacent token pairs
    joined with underscores, so 'sick leave' matches entity
    'sick_leave' exactly. Never substrings: 'rule' must not match
    'frequency_rule'."""
    keys = set(tokens)
    for a, b in zip(tokens, tokens[1:]):
        keys.add(a + "_" + b)
    return keys


def fact_covers_question(fact_text: str, question: str) -> bool:
    """True when the fact text contains any coverage key as a whole word."""
    keys = key_tokens(question)
    if not keys:
        return False
    words = set(_TOKEN_RE.findall((fact_text or "").lower()))
    return any(k in words for k in keys)


def filter_covering(facts: list, question: str) -> list:
    """Keep only facts whose text covers the question's key tokens."""
    return [f for f in facts if fact_covers_question(f.get("fact", ""), question)]


def _row_to_entry(row, document_ids, scores=None):
    eff = row["effective_date"]
    entry = {
        "id": str(row["id"]),
        "fact": row["fact"],
        "version": row["version"],
        "is_latest": row["is_latest"],
        "status": row["status"],
        "effective_date": eff.isoformat() if eff is not None else None,
        "visible_to": list(row["visible_to"] or []),
        "source": row["source"],
        "source_message_id": row["source_message_id"],
        "document_ids": [str(d) for d in document_ids],
    }
    if scores is not None:
        entry["scores"] = scores
    return entry


def _fetch_documents(conn, memory_ids):
    """memory_id (str) -> list of document UUIDs from memory_sources."""
    docs = {}
    if not memory_ids:
        return docs
    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id, document_id FROM memory_sources "
            "WHERE memory_id = ANY(%s) AND document_id IS NOT NULL",
            (memory_ids,),
        )
        for mid, doc_id in cur.fetchall():
            docs.setdefault(str(mid), []).append(doc_id)
    return docs


def search(conn, company_id, role, query, include_superseded=False, limit=10):
    """Candidate UNION of keyword, semantic and entity paths, then rank.

    Ranking (mem0 score_and_rank shape): raw = semantic + keyword
    (expanded-token match count, normalized) + entity,
    final = min(1, raw / max_possible), combined = final + recency
    - superseded_penalty (penalty only when include_superseded=True
    lets a superseded row through). Semantic signal below
    SEMANTIC_THRESHOLD is gated first: it contributes nothing, and a
    candidate with no keyword/entity signal at all is dropped.
    """
    filt = _base_filter(include_superseded, role)
    # id (str) -> {"semantic": float, "keyword": float, "entity": bool}
    cand = {}
    expanded = expand_tokens(_tokens(query))

    with conn.cursor() as cur:
        # (1) Keyword: OR over expanded tokens — a fact matching ANY
        # query word is a candidate. The old plainto_tsquery AND-match
        # returned nothing for "Rule for sl": real SL facts don't
        # contain the word "rule". Ranking by match count happens in
        # Python below, from the lemmatized text.
        if expanded:
            tsquery = " | ".join(expanded)  # tokens are [a-z0-9]+ only
            cur.execute(
                "SELECT id FROM memories "
                f"WHERE {filt} "
                "AND to_tsvector('simple', COALESCE(text_lemmatized, '')) "
                "@@ to_tsquery('simple', %s)",
                (company_id, tsquery),
            )
            for (mid,) in cur.fetchall():
                cand.setdefault(
                    str(mid), {"semantic": 0.0, "keyword": 0.0, "entity": 0.0}
                )

        # (2) Semantic, ONLY when vectors are actually available. This is
        # the PRIMARY path; keyword below is the always-on fallback.
        query_vec = None
        try:
            if embed.vectors_available():
                query_vec = embed.embed_query(query)
        except ValueError:
            query_vec = None  # over-long input -> no vector, per contract
        except Exception:
            query_vec = None
        semantic_active = query_vec is not None
        if query_vec is not None:
            vec_literal = "[" + ",".join(repr(float(x)) for x in query_vec) + "]"
            cur.execute(
                "SELECT id, embedding <=> %s::vector AS dist "
                "FROM memories "
                f"WHERE {filt} AND embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector "
                "LIMIT %s",
                (vec_literal, company_id, vec_literal, SEMANTIC_LIMIT),
            )
            for mid, dist in cur.fetchall():
                entry = cand.setdefault(
                    str(mid), {"semantic": 0.0, "keyword": 0.0, "entity": 0.0}
                )
                entry["semantic"] = max(0.0, 1.0 - float(dist))

        # (3) Entity 1-hop with hub damping (mem0 rule): an entity linked
        # to many memories is worth less than a rare one. Per memory,
        # boost = MAX over matched entities of
        #   sim * 0.5 * 1 / (1 + 0.001 * (n_linked - 1)^2)
        # for matches with sim >= 0.5. sim = 1.0 on exact whole-name
        # equality only — there is no substring tier, so "rule" never
        # matches "frequency_rule". The ILIKE prefilter below only
        # over-fetches candidates; the exact gate in Python decides.
        if expanded:
            clauses = []
            params = [company_id]
            for tok in expanded:
                clauses.append("(subject ILIKE %s OR object ILIKE %s)")
                params.extend([f"%{tok}%", f"%{tok}%"])
            cur.execute(
                "SELECT memory_id, subject, object FROM edges "
                "WHERE company_id = %s AND (" + " OR ".join(clauses) + ")",
                params,
            )
            # entity key -> set of memory ids; (memory, entity) -> best sim
            entity_members = {}
            mem_entity_sim = {}
            keys = _entity_keys(expanded)
            for mid, subject, obj in cur.fetchall():
                for ent in (subject or "", obj or ""):
                    ent_norm = ent.lower()
                    if ent_norm not in keys:
                        continue
                    sim = 1.0
                    if sim < ENTITY_SIM_MIN:
                        continue
                    entity_members.setdefault(ent_norm, set()).add(str(mid))
                    key = (str(mid), ent_norm)
                    mem_entity_sim[key] = max(mem_entity_sim.get(key, 0.0), sim)
            for (mid, ent_norm), sim in mem_entity_sim.items():
                n_linked = len(entity_members.get(ent_norm, set()))
                damped = sim * 0.5 * (1.0 / (1.0 + 0.001 * (n_linked - 1) ** 2))
                entry = cand.setdefault(
                    str(mid), {"semantic": 0.0, "keyword": 0.0, "entity": 0.0}
                )
                entry["entity"] = max(entry["entity"], damped)

    if not cand:
        return []

    # Final fetch applies the base filter to every candidate, so an
    # entity-linked row that is hidden, forgotten, expired or out of
    # company scope can never leak through.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, fact, version, is_latest, status, effective_date, "
            "visible_to, source, source_message_id, text_lemmatized FROM memories "
            f"WHERE id = ANY(%s::uuid[]) AND {filt}",
            (list(cand.keys()), company_id),
        )
        rows = cur.fetchall()
        cols = [d.name for d in cur.description]

    docs = _fetch_documents(conn, [r[0] for r in rows])
    today = date.today()
    # Keyword score = how many expanded query tokens the fact's
    # lemmatized text contains as whole words (OR-match ranking:
    # more matched words outranks fewer).
    for row in rows:
        rec0 = dict(zip(cols, row))
        lem_words = set(_TOKEN_RE.findall((rec0.get("text_lemmatized") or "").lower()))
        cand[str(rec0["id"])]["keyword"] = float(
            sum(1 for t in expanded if t in lem_words)
        )
    max_rank = max((c["keyword"] for c in cand.values()), default=0.0)
    fetched = [cand[str(r[0])] for r in rows]
    any_bm25 = max_rank > 0
    any_entity = any(c["entity"] > 0 for c in fetched)
    max_possible = 1.0 + (1.0 if any_bm25 else 0.0) + (0.5 if any_entity else 0.0)

    results = []
    for row in rows:
        rec = dict(zip(cols, row))
        mid = str(rec["id"])
        c = cand[mid]
        keyword_norm = (c["keyword"] / max_rank) if max_rank > 0 else 0.0
        entity_boost = float(c["entity"])
        # Semantic threshold gates FIRST: a sub-threshold semantic
        # signal contributes nothing. A candidate with no keyword or
        # entity signal either is dropped outright (the union fix for
        # mem0's semantic-only candidate set keeps keyword/entity
        # matches alive even when their semantic score is low).
        semantic_used = c["semantic"] if c["semantic"] >= SEMANTIC_THRESHOLD else 0.0
        if (
            semantic_active
            and c["semantic"] < SEMANTIC_THRESHOLD
            and c["keyword"] == 0
            and entity_boost == 0
        ):
            continue
        eff = rec["effective_date"]
        recency = RECENCY_BOOST if (eff is not None and eff <= today) else 0.0
        penalty = SUPERSEDED_PENALTY if rec["status"] == "superseded" else 0.0
        raw_score = semantic_used + keyword_norm + entity_boost
        final_score = min(1.0, raw_score / max_possible) if max_possible > 0 else 0.0
        combined = final_score + recency - penalty
        scores = {
            "semantic": semantic_used,
            "keyword": keyword_norm,
            "entity": entity_boost,
            "combined": combined,
            "score_details": {
                "semantic_score": semantic_used,
                "bm25_score": keyword_norm,
                "entity_boost": entity_boost,
                "raw_score": raw_score,
                "max_possible_score": max_possible,
                "final_score": final_score,
                "threshold": SEMANTIC_THRESHOLD,
            },
        }
        results.append(_row_to_entry(rec, docs.get(mid, []), scores))

    results.sort(key=lambda e: e["scores"]["combined"], reverse=True)
    if limit is not None:
        results = results[:limit]
    return results


def get_timeline(conn, company_id, memory_id):
    """Walk parent_memory_id back to the root, return root-first.

    Cycle-guarded (max 100 steps); every step must belong to company_id,
    otherwise the walk stops.
    """
    chain = []
    seen = set()
    current = str(memory_id)
    with conn.cursor() as cur:
        for _ in range(100):
            if current in seen:
                break
            seen.add(current)
            cur.execute(
                "SELECT id, fact, version, effective_date, status, source, "
                "source_message_id, created_at, parent_memory_id FROM memories "
                "WHERE id = %s AND company_id = %s",
                (current, company_id),
            )
            row = cur.fetchone()
            if row is None:
                break
            mid, fact, version, eff, status, source, src_msg, created, parent = row
            chain.append(
                {
                    "id": str(mid),
                    "fact": fact,
                    "version": version,
                    "effective_date": eff.isoformat() if eff is not None else None,
                    "status": status,
                    "source": source,
                    "source_message_id": src_msg,
                    "created_at": created.isoformat() if created is not None else None,
                }
            )
            if parent is None:
                break
            current = str(parent)
    chain.reverse()
    return chain


def get_memory(conn, memory_id):
    """Single row, same shape as search entries minus scores."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, fact, version, is_latest, status, effective_date, "
            "visible_to, source, source_message_id FROM memories "
            "WHERE id = %s",
            (str(memory_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description]
        rec = dict(zip(cols, row))
    docs = _fetch_documents(conn, [rec["id"]])
    return _row_to_entry(rec, docs.get(str(rec["id"]), []))


def search_as_of(conn, company_id, role, query, as_of, limit=10):
    """What was true on date X: same candidate union as search(), but
    temporal filtering replaces the active-latest default.

    as_of is a date or 'YYYY-MM-DD' string. Per fact family (root id),
    the winner is the version valid at as_of: greatest effective_date
    <= as_of, else (no effective_date) greatest created_at <= end of
    as_of day. Forgotten rows and rows past forget_after at as_of are
    excluded. No superseded penalty applies: at as_of time the winner
    WAS current. Returns the same entry shape as search().
    """
    if isinstance(as_of, str):
        as_of = date.fromisoformat(as_of)
    filt = (
        "company_id = %s "
        f"AND {_role_predicate(role)} "
        "AND is_forgotten = false "
        "AND (forget_after IS NULL OR forget_after > %s::date)"
    )
    cand = {}
    expanded = expand_tokens(_tokens(query))
    with conn.cursor() as cur:
        # Keyword: OR over expanded tokens (see search()).
        if expanded:
            tsquery = " | ".join(expanded)  # tokens are [a-z0-9]+ only
            cur.execute(
                "SELECT id FROM memories "
                f"WHERE {filt} "
                "AND to_tsvector('simple', COALESCE(text_lemmatized, '')) "
                "@@ to_tsquery('simple', %s)",
                (company_id, as_of, tsquery),
            )
            for (mid,) in cur.fetchall():
                cand.setdefault(
                    str(mid), {"semantic": 0.0, "keyword": 0.0, "entity": 0.0}
                )
        query_vec = None
        try:
            if embed.vectors_available():
                query_vec = embed.embed_query(query)
        except Exception:
            query_vec = None
        semantic_active = query_vec is not None
        if query_vec is not None:
            vec_literal = "[" + ",".join(repr(float(x)) for x in query_vec) + "]"
            cur.execute(
                "SELECT id, embedding <=> %s::vector AS dist "
                "FROM memories "
                f"WHERE {filt} AND embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector "
                "LIMIT %s",
                (vec_literal, company_id, as_of, vec_literal, SEMANTIC_LIMIT),
            )
            for mid, dist in cur.fetchall():
                entry = cand.setdefault(
                    str(mid), {"semantic": 0.0, "keyword": 0.0, "entity": 0.0}
                )
                entry["semantic"] = max(0.0, 1.0 - float(dist))
        if expanded:
            clauses = []
            params = [company_id]
            for tok in expanded:
                clauses.append("(subject ILIKE %s OR object ILIKE %s)")
                params.extend([f"%{tok}%", f"%{tok}%"])
            cur.execute(
                "SELECT memory_id, subject, object FROM edges "
                "WHERE company_id = %s AND (" + " OR ".join(clauses) + ")",
                params,
            )
            entity_members = {}
            mem_entity_sim = {}
            keys = _entity_keys(expanded)
            for mid, subject, obj in cur.fetchall():
                for ent in (subject or "", obj or ""):
                    ent_norm = ent.lower()
                    # Whole-name match only (see search()).
                    if ent_norm not in keys:
                        continue
                    sim = 1.0
                    if sim < ENTITY_SIM_MIN:
                        continue
                    entity_members.setdefault(ent_norm, set()).add(str(mid))
                    key = (str(mid), ent_norm)
                    mem_entity_sim[key] = max(mem_entity_sim.get(key, 0.0), sim)
            for (mid, ent_norm), sim in mem_entity_sim.items():
                n_linked = len(entity_members.get(ent_norm, set()))
                damped = sim * 0.5 * (1.0 / (1.0 + 0.001 * (n_linked - 1) ** 2))
                entry = cand.setdefault(
                    str(mid), {"semantic": 0.0, "keyword": 0.0, "entity": 0.0}
                )
                entry["entity"] = max(entry["entity"], damped)
    if not cand:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, fact, version, is_latest, status, effective_date, "
            "visible_to, source, source_message_id, root_memory_id, "
            "created_at, text_lemmatized FROM memories "
            f"WHERE id = ANY(%s::uuid[]) AND {filt}",
            (list(cand.keys()), company_id, as_of),
        )
        rows = cur.fetchall()
        cols = [d.name for d in cur.description]
    # Family pick: one winner per root family, valid at as_of.
    families = {}
    for row in rows:
        rec = dict(zip(cols, row))
        family = str(rec["root_memory_id"] or rec["id"])
        eff = rec["effective_date"]
        created = rec["created_at"]
        valid = (eff is not None and eff <= as_of) or (
            eff is None and created is not None and created.date() <= as_of
        )
        if not valid:
            continue
        sort_key = (
            1 if eff is not None else 0,
            eff or date.min,
            created.isoformat() if created is not None else "",
        )
        prev = families.get(family)
        if prev is None or sort_key > prev[0]:
            families[family] = (sort_key, rec)
    winners = [rec for _, rec in families.values()]
    docs = _fetch_documents(conn, [r["id"] for r in winners])
    # Keyword score = expanded-token match count (see search()).
    for rec in winners:
        lem_words = set(
            _TOKEN_RE.findall((rec.get("text_lemmatized") or "").lower())
        )
        cand[str(rec["id"])]["keyword"] = float(
            sum(1 for t in expanded if t in lem_words)
        )
    max_rank = max((c["keyword"] for c in cand.values()), default=0.0)
    any_bm25 = max_rank > 0
    any_entity = any(cand[str(r["id"])]["entity"] > 0 for r in winners)
    max_possible = 1.0 + (1.0 if any_bm25 else 0.0) + (0.5 if any_entity else 0.0)
    results = []
    for rec in winners:
        mid = str(rec["id"])
        c = cand[mid]
        keyword_norm = (c["keyword"] / max_rank) if max_rank > 0 else 0.0
        entity_boost = float(c["entity"])
        semantic_used = c["semantic"] if c["semantic"] >= SEMANTIC_THRESHOLD else 0.0
        if (
            semantic_active
            and c["semantic"] < SEMANTIC_THRESHOLD
            and c["keyword"] == 0
            and entity_boost == 0
        ):
            continue
        eff = rec["effective_date"]
        recency = RECENCY_BOOST if (eff is not None and eff <= as_of) else 0.0
        raw_score = semantic_used + keyword_norm + entity_boost
        final_score = min(1.0, raw_score / max_possible) if max_possible > 0 else 0.0
        combined = final_score + recency
        scores = {
            "semantic": semantic_used,
            "keyword": keyword_norm,
            "entity": entity_boost,
            "combined": combined,
            "score_details": {
                "semantic_score": semantic_used,
                "bm25_score": keyword_norm,
                "entity_boost": entity_boost,
                "raw_score": raw_score,
                "max_possible_score": max_possible,
                "final_score": final_score,
                "threshold": SEMANTIC_THRESHOLD,
            },
        }
        results.append(_row_to_entry(rec, docs.get(mid, []), scores))
    results.sort(key=lambda e: e["scores"]["combined"], reverse=True)
    if limit is not None:
        results = results[:limit]
    return results
