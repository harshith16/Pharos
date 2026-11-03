"""Local EmbeddingGemma embeddings through Ollama — OPTIONAL, additive only.

Retrieval works fully without this module (Postgres full-text). When Ollama
with the embeddinggemma model is present, vectors enrich ranking; when it is
absent, every function degrades to None and the app still fully works.

Design rules enforced here (from the repo research):
- Asymmetric prefixes are mandatory. A bare encode() with no prefix is NOT
  shipped retrieval behaviour, and Ollama adds no prefixes by itself.
- One embedding column, one producer. Never mix Ollama and
  sentence-transformers vectors in the same column.
- Inputs over the 2048-token window are FLAGGED (ValueError), never
  silently truncated. Callers must chunk first; store.py treats a
  ValueError from here as "no vector, store NULL".
- Vectors are L2-normalized here and the norm is verified.
"""

import math

import httpx

from . import config

MAX_TOKENS = 2048
# Conservative estimate: ~4 chars per token. Anything above the budget is
# refused so a chunk can never be silently cut by the model server.
MAX_CHARS = MAX_TOKENS * 4


def document_text(title: str, text: str) -> str:
    return f"title: {title} | text: {text}"


def query_text(query: str) -> str:
    return f"task: question answering | query: {query}"


def _check_budget(text: str) -> None:
    if len(text) > MAX_CHARS:
        raise ValueError(
            f"embedding input too long: ~{len(text) // 4} tokens estimated, "
            f"limit is {MAX_TOKENS}. Chunk the input first; refusing to truncate."
        )


def _normalize(vec: list) -> list:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        raise ValueError("embedding server returned a zero vector")
    return [x / norm for x in vec]


import time

_client = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.Client(trust_env=False)
    return _client


_AVAIL_CACHE = {"status": False, "checked_at": 0.0}
_AVAIL_TTL = 30.0  # Cache availability for 30s instead of pinging /api/tags every query


def vectors_available(timeout: float = 2.0, force: bool = False) -> bool:
    """True only if Ollama is reachable AND the embed model is installed.

    Cached with a 30s TTL to prevent massive multi-second delay on every retrieval step.
    """
    now = time.time()
    if not force and (now - _AVAIL_CACHE["checked_at"]) < _AVAIL_TTL:
        return _AVAIL_CACHE["status"]

    try:
        client = _get_client()
        resp = client.get(f"{config.OLLAMA_URL}/api/tags", timeout=timeout)
        resp.raise_for_status()
        models = [m.get("name", "") for m in resp.json().get("models", [])]
        status = any(
            m == config.EMBED_MODEL or m.startswith(config.EMBED_MODEL + ":")
            for m in models
        )
    except Exception:
        status = False

    _AVAIL_CACHE["status"] = status
    _AVAIL_CACHE["checked_at"] = now
    return status


def embed(text: str, timeout: float = 60.0) -> list | None:
    """Embed one already-prefixed string. Returns a 768-d unit vector,
    or None when no vector can be produced (Ollama absent, model missing,
    error, wrong dims). Never raises for server problems; the token-budget
    ValueError is the one exception, by design (callers must chunk)."""
    _check_budget(text)
    try:
        client = _get_client()
        resp = client.post(
            f"{config.OLLAMA_URL}/api/embeddings",
            json={"model": config.EMBED_MODEL, "prompt": text},
            timeout=timeout,
        )
        resp.raise_for_status()
        vec = resp.json()["embedding"]
    except Exception:
        return None
    if len(vec) != config.EMBED_DIMS:
        return None
    try:
        vec = _normalize(vec)
    except ValueError:
        return None
    norm = math.sqrt(sum(x * x for x in vec))
    if abs(norm - 1.0) > 1e-3:
        return None
    return vec


def embed_document(title: str, text: str) -> list | None:
    return embed(document_text(title, text))


_QUERY_CACHE: dict[str, list] = {}


def embed_query(query: str) -> list | None:
    """Embed a query text with in-memory caching to avoid duplicate embeddings
    for the same question in router, retrieval, and sensitive checking."""
    if query in _QUERY_CACHE:
        return _QUERY_CACHE[query]
    vec = embed(query_text(query))
    if vec is not None:
        if len(_QUERY_CACHE) > 200:
            _QUERY_CACHE.clear()
        _QUERY_CACHE[query] = vec
    return vec
