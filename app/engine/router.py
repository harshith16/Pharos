"""Engine-decided routing: for each question, THE ENGINE DECIDES what
answers it. Nothing here is a hand-typed domain list.

The router computes, from engine data only:
- which stored entities the question refers to (resolved against graph
  entity names AND aliases that uploads themselves taught the engine),
- how many covering facts exist (legacy retrieval, role-filtered,
  gated to facts that actually contain the question's key tokens),
- whether a role-filtered graph path exists,
- whether covering facts exist at CEO scope but are hidden from this
  role (role_blocked: counted, never leaked),
- freshness: the latest effective date among covering facts.

From those signals it routes to exactly one of:
- ENGINE: answer from retrieved uploaded facts, with evidence/sources.
- NOT_FOUND: a factual question with no covering facts — the plain
  reply that it could not be found in the company documents. Never a
  world-knowledge answer.
- LLM_DIRECT: a short non-question utterance with no engine coverage
  (a greeting or pleasantry). The model reply must make no factual
  claim; it is produced by app.llm.generate_smalltalk.
- ACT: an action request ("inform HR about my reimbursement"). The
  action verb is generic language, but the TARGET and the TOPIC both
  resolve against engine data: the target is a stored entity the
  engine knows as an approver (source of an approves-type relation
  learned from uploads), and the topic has role-filtered covering
  facts. The router only decides ACT; app.actions executes the real
  email send, and the WhatsApp layer only sends the resulting reply.

The split between NOT_FOUND and LLM_DIRECT for zero-coverage messages
uses ONLY message structure, never vocabulary: a question mark, a
generic English interrogative word, or a message of 3+ words means
factual intent (NOT_FOUND); anything shorter without a question marker
is small-talk (LLM_DIRECT). The interrogative set is linguistic
structure, not product knowledge: it says nothing about what the
question is about.

The WhatsApp layer calls this module and sends what it decides; it
makes no routing decision of its own. Role gating itself is untouched:
role filtering happens inside the retrieval and graph queries, exactly
as before. Importing this module needs no database connection and no
LLM key; missing data yields NOT_FOUND or LLM_DIRECT, never an
exception.
"""

import logging
import re

from app import retrieve
from app.engine import query as engine_query

try:  # Legacy answer path, used only for the ENGINE route's facts.
    from app import answer as _legacy_answer
except ImportError:  # pragma: no cover - parallel-build tolerance
    _legacy_answer = None

try:  # Small-talk model reply for the LLM_DIRECT route.
    from app import llm
except ImportError:  # pragma: no cover - parallel-build tolerance
    llm = None

NOT_FOUND_REPLY = "I couldn't find that in the company documents."

ENGINE = "ENGINE"
NOT_FOUND = "NOT_FOUND"
LLM_DIRECT = "LLM_DIRECT"
ACT = "ACT"

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Generic action verbs. Linguistic structure only, like the
# interrogative set: these mark a message as a request to do
# something, they carry no domain or product facts. The target and
# topic are never taken from a list — both resolve from stored rows.
_ACTION_VERBS = {
    "inform",
    "tell",
    "email",
    "mail",
    "notify",
    "message",
    "contact",
    "alert",
    "send",
}

# Generic English interrogatives. Linguistic structure only: these
# mark a message as a question, they carry no domain or product facts.
_QUESTION_WORDS = {
    "what",
    "how",
    "when",
    "where",
    "who",
    "why",
    "which",
    "whose",
    "whom",
}

_log = logging.getLogger("engine_router")


def _normalize_name(name: str) -> str:
    """Lowercased, whitespace-collapsed name; underscores count as spaces."""
    return " ".join(str(name).replace("_", " ").strip().lower().split())


def load_alias_map(conn, company_id) -> dict:
    """Alias token -> entity name tokens, built ONLY from stored rows.

    Every entry comes from an engine_entities row: the entity's stored
    name, plus each stored alias. Only single-token aliases map (the
    abbreviation case: an alias learned from an upload, e.g. an entity
    named "Sick Leave" carrying alias "SL", maps token "sl" to
    ("sick", "leave")). Multi-word aliases resolve through whole-name
    entity matching instead. An empty map means no upload has taught
    any alias yet — expansion then adds nothing, and that is correct.
    """
    alias_map = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, display_name, aliases FROM engine_entities "
            "WHERE company_id = %s",
            (company_id,),
        )
        for name, display, aliases in cur.fetchall():
            base = None
            if isinstance(display, str) and display.strip():
                base = display
            elif isinstance(name, str) and name.strip():
                base = name
            if base is None:
                continue
            name_tokens = tuple(_normalize_name(base).split())
            if not name_tokens:
                continue
            forms = []
            if isinstance(aliases, (list, tuple)):
                forms = [a for a in aliases if isinstance(a, str)]
            elif isinstance(aliases, str) and aliases.strip():
                forms = [aliases]
            for alias in forms:
                toks = _normalize_name(alias).split()
                if len(toks) == 1 and toks[0] not in name_tokens:
                    alias_map.setdefault(toks[0], name_tokens)
    return alias_map


def _has_question_marker(question: str) -> bool:
    """True when the message is shaped like a question: a '?' or a
    generic interrogative word. Says nothing about the topic."""
    text = question or ""
    if "?" in text:
        return True
    words = set(_TOKEN_RE.findall(text.lower()))
    return bool(words & _QUESTION_WORDS)


def _word_count(question: str) -> int:
    return len(_TOKEN_RE.findall(question or ""))


def _link_entities_by_token(conn, company_id, message) -> list:
    """Stored entities whose name/alias tokens appear in the message.

    Whole-word token overlap only, in order of first appearance in
    the message. This is how "inform HR about my reimbursement" finds
    the stored entity HR Lead via its hr token even though the full name
    never appears in the message. Every candidate comes from a stored
    row — nothing is invented, and no role list is consulted.
    """
    words = _TOKEN_RE.findall((message or "").lower())
    if not words:
        return []
    positions = {}
    for i, word in enumerate(words):
        positions.setdefault(word, i)
    hits = []  # (first_pos, display)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, display_name, aliases FROM engine_entities "
                "WHERE company_id = %s",
                (company_id,),
            )
            rows = cur.fetchall()
    except Exception:
        return []
    for name, display, aliases in rows:
        shown = None
        if isinstance(display, str) and display.strip():
            shown = display.strip()
        elif isinstance(name, str) and name.strip():
            shown = name.strip()
        if not shown:
            continue
        forms = [shown]
        if isinstance(aliases, (list, tuple)):
            forms += [a for a in aliases if isinstance(a, str)]
        elif isinstance(aliases, str) and aliases.strip():
            forms.append(aliases)
        best = None
        for form in forms:
            for tok in _TOKEN_RE.findall(form.lower()):
                if tok in positions and (best is None or positions[tok] < best):
                    best = positions[tok]
        if best is not None:
            hits.append((best, shown))
    hits.sort()
    out = []
    seen = set()
    for _pos, shown in hits:
        if shown not in seen:
            seen.add(shown)
            out.append(shown)
    return out


def _approver_entities(conn, company_id) -> set:
    """Display names the engine knows as approvers, from stored rows.

    An approver is an entity that is the SOURCE of an approves-type
    relation (rel "approves", or any rel containing the "approv"
    stem, which covers LLM-extracted triples like "approved_by").
    The relation exists only because an upload said so — e.g. "The
    HR Lead approves the Reimbursement Policy." No role list is
    consulted; an entity nobody's upload made an approver of can
    never become an email target.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT e.display_name FROM engine_relations r "
                "JOIN engine_entities e ON e.id = r.src_id "
                "WHERE r.company_id = %s "
                "AND (r.rel = 'approves' OR r.rel LIKE '%%approv%%')",
                (company_id,),
            )
            return {row[0] for row in cur.fetchall() if row and row[0]}
    except Exception:
        return set()


def _detect_act(conn, company_id, role, question, alias_map) -> dict | None:
    """ACT detection, entirely from engine data signals.

    Returns the ACT route dict, or None when the message is not an
    action the engine can ground: no generic action verb, no
    role-filtered covering facts for the topic, or no stored
    approver entity mentioned in the message. Never raises.
    """
    try:
        tokens = set(_TOKEN_RE.findall((question or "").lower()))
        verbs = sorted(tokens & _ACTION_VERBS)
        if not verbs:
            return None
        try:
            found = retrieve.search(
                conn, company_id, role, question, alias_map=alias_map
            )
            covering = retrieve.filter_covering(
                found, question, alias_map=alias_map
            )
        except Exception:
            return None
        if not covering:
            return None
        candidates = _link_entities_by_token(conn, company_id, question)
        approvers = _approver_entities(conn, company_id)
        target = None
        for candidate in candidates:
            if candidate in approvers:
                target = candidate
                break
        if target is None:
            return None
        topic = None
        for candidate in candidates:
            if candidate != target:
                topic = candidate
                break
        if topic is None:
            topic = "your request"
        citations = []
        try:
            citations = engine_query._build_citations(covering, conn)
        except Exception:
            citations = []
        signals = {
            "action_verbs": verbs,
            "entities_matched": candidates,
            "target": target,
            "topic": topic,
            "covering_fact_count": len(covering),
            "question_marker": _has_question_marker(question),
            "word_count": _word_count(question),
        }
        return {
            "answer": "",
            "facts": covering,
            "citations": citations,
            "path": [],
            "empty": False,
            "source": "router",
            "route": ACT,
            "target_display": target,
            "topic": topic,
            "signals": signals,
        }
    except Exception:
        return None


def route_question(conn, company_id, role, question) -> dict:
    """Decide the route for one question, from engine data signals.

    Returns a dict with "route" (ENGINE, NOT_FOUND, or LLM_DIRECT),
    "signals" (the data that drove the decision, so a test can prove
    it came from stored rows and not from a word list), and, for the
    ENGINE route, "answer", "facts", "citations" and "path". For
    NOT_FOUND the answer is the plain not-found text. For LLM_DIRECT
    the answer is "" here — the model reply is produced by
    answer_question(), not by routing (routing itself needs no key).
    Never raises for missing data.
    """
    question = question or ""
    try:
        alias_map = load_alias_map(conn, company_id)
    except Exception:
        alias_map = {}
    question_tokens = set(_TOKEN_RE.findall(question.lower()))
    expansions_used = sorted(t for t in alias_map if t in question_tokens)

    # (0) ACT first: an action request grounded in engine data wins
    # over answering — "inform HR about my reimbursement" is a
    # request to do something, not a question to answer. A message
    # that is not a grounded action falls through to the routes below.
    act = _detect_act(conn, company_id, role, question, alias_map)
    if act is not None:
        _log.info("route=%s signals=%s", ACT, act["signals"])
        return act

    entities_matched = []
    try:
        entities_matched = engine_query._link_entities(conn, company_id, question)
    except Exception:
        entities_matched = []

    # (1) Engine graph answer first: a role-filtered path, with facts
    # when a key is present for synthesis.
    engine_result = None
    try:
        engine_result = engine_query.answer_with_graph(
            conn, company_id, role, question, alias_map=alias_map
        )
    except Exception:
        engine_result = None
    if isinstance(engine_result, dict) and not engine_result.get("empty"):
        path = engine_result.get("path") or []
        facts = engine_result.get("facts") or []
        dates = sorted(
            {f.get("effective_date") for f in facts if f.get("effective_date")}
        )
        signals = {
            "entities_matched": entities_matched,
            "covering_fact_count": len(facts),
            "engine_path": bool(path),
            "engine_path_hops": len(path),
            "role_blocked": False,
            "freshness": dates[-1] if dates else None,
            "question_marker": _has_question_marker(question),
            "word_count": _word_count(question),
            "alias_expansions_used": expansions_used,
        }
        out = dict(engine_result)
        out["route"] = ENGINE
        out["signals"] = signals
        _log.info("route=%s signals=%s", ENGINE, signals)
        return out

    # (2) Legacy covering facts: role-filtered search, gated to facts
    # that actually contain the question's key tokens.
    covering = []
    try:
        found = retrieve.search(
            conn, company_id, role, question, alias_map=alias_map
        )
        covering = retrieve.filter_covering(found, question, alias_map=alias_map)
    except Exception:
        covering = []

    # Role signal: covering facts exist at CEO scope but are hidden
    # from this role. Counted only — their content is never returned.
    role_blocked = False
    if not covering and role != "ceo":
        try:
            ceo_found = retrieve.search(
                conn, company_id, "ceo", question, alias_map=alias_map
            )
            role_blocked = (
                len(
                    retrieve.filter_covering(
                        ceo_found, question, alias_map=alias_map
                    )
                )
                > 0
            )
        except Exception:
            role_blocked = False

    dates = sorted(
        {f.get("effective_date") for f in covering if f.get("effective_date")}
    )
    signals = {
        "entities_matched": entities_matched,
        "covering_fact_count": len(covering),
        "engine_path": False,
        "engine_path_hops": 0,
        "role_blocked": role_blocked,
        "freshness": dates[-1] if dates else None,
        "question_marker": _has_question_marker(question),
        "word_count": _word_count(question),
        "alias_expansions_used": expansions_used,
    }

    if covering:
        legacy = None
        if _legacy_answer is not None:
            try:
                legacy = _legacy_answer.answer_question(
                    conn, company_id, role, question, alias_map=alias_map
                )
            except Exception:
                legacy = None
        if isinstance(legacy, dict) and not legacy.get("empty"):
            out = dict(legacy)
            out["route"] = ENGINE
            out["signals"] = signals
            _log.info("route=%s signals=%s", ENGINE, signals)
            return out
        # Covering facts exist but no synthesis was possible (e.g. no
        # LLM key on this machine). The route is still ENGINE, decided
        # by data — the facts are the answer's evidence, and no
        # not-found text is sent for a question the data does cover.
        out = {
            "answer": "",
            "facts": covering,
            "citations": engine_query._build_citations(covering, conn),
            "path": [],
            "empty": False,
            "source": "legacy",
            "route": ENGINE,
            "signals": signals,
        }
        _log.info("route=%s signals=%s", ENGINE, signals)
        return out

    # (3) No coverage anywhere: factual shape -> NOT_FOUND, short
    # non-question utterance -> LLM_DIRECT.
    if _has_question_marker(question) or _word_count(question) >= 3:
        out = {
            "answer": NOT_FOUND_REPLY,
            "facts": [],
            "citations": [],
            "path": [],
            "empty": True,
            "source": "router",
            "route": NOT_FOUND,
            "signals": signals,
        }
        _log.info("route=%s signals=%s", NOT_FOUND, signals)
        return out
    out = {
        "answer": "",
        "facts": [],
        "citations": [],
        "path": [],
        "empty": True,
        "source": "router",
        "route": LLM_DIRECT,
        "signals": signals,
    }
    _log.info("route=%s signals=%s", LLM_DIRECT, signals)
    return out


def answer_question(conn, company_id, role, question) -> dict:
    """Route a question and produce the reply text for its route.

    ENGINE: the engine/legacy answer (facts + evidence). NOT_FOUND:
    the plain not-found text. LLM_DIRECT: a small-talk model reply
    that makes no factual claim, or "" when no key is set or the call
    fails (the caller then sends nothing rather than a fabrication).
    ACT: returned as-is — the router decides, the caller executes
    the action (app.actions) and produces the confirmation reply.
    """
    result = route_question(conn, company_id, role, question)
    if result["route"] == LLM_DIRECT:
        reply = ""
        if llm is not None:
            try:
                reply = llm.generate_smalltalk(question) or ""
            except Exception:
                reply = ""
        out = dict(result)
        out["answer"] = reply
        out["empty"] = not bool(reply.strip())
        return out
    if result["route"] == ENGINE:
        # As-of questions get the deterministic version-chain
        # answer (both values with source tags) instead of a
        # synthesis that might mention only one version. No key
        # needed: timelines are stored data.
        try:
            from app import asof as asof_mod

            if asof_mod.is_asof_question(question):
                text = asof_mod.asof_answer(
                    conn, company_id, result.get("facts") or []
                )
                if text:
                    out = dict(result)
                    out["answer"] = text
                    return out
        except Exception:
            pass
    return result
