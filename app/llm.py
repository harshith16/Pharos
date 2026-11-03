"""Model client: fact extraction + answer synthesis.

Provider switch (P1-6): LLM_PROVIDER=meta|ollama, default meta,
selected once at import (a server restart picks it up). "meta"
is the Meta Model API; "ollama" is a local open-weight Llama
model served by Ollama's OpenAI-compatible endpoint
(OLLAMA_URL + "/v1", model OLLAMA_MODEL, default llama3.2:3b).
Every function below keeps its signature and prompts; only the
base URL, model, and key change, so answers, extraction, and
composition all work through the same code on either provider.
Vision under ollama uses OLLAMA_VISION_MODEL (default moondream),
because a text Llama model cannot read images.

Only fictional sample-company data ever goes through the Meta API.
This is required by the free-credit product-improvement clause: content
sent on free credits may be used for product improvement, so no real
company data is ever sent here. This module never sees real company
data; callers pass only sample-company text.

No secrets in code, logs, or output: the API key is read from the
environment only, and is never printed or logged, nor is the
Authorization header.
"""

import json
import os
import re
from datetime import datetime

import httpx

from . import config

# General fact: visible to every hierarchy role (employee, CFO, CTO, CEO).
_DEFAULT_VISIBLE_TO = ["employee", "cfo", "cto", "ceo"]
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalize_provider(value) -> str:
    """LLM_PROVIDER to "meta" or "ollama"; anything else (including
    empty) falls back to meta, never crashes startup."""
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in ("meta", "ollama") else "meta"


# Selected once at import (server startup). Tests may assign
# app.llm.PROVIDER directly; production changes need a restart.
PROVIDER = _normalize_provider(os.environ.get("LLM_PROVIDER", ""))


def _ollama_base() -> str:
    return (
        os.environ.get("OLLAMA_URL")
        or config.OLLAMA_URL
        or "http://localhost:11434"
    ).rstrip("/")


def _api_key() -> str:
    if PROVIDER == "ollama":
        # Ollama ignores auth; the placeholder only keeps the
        # client's key checks (and has_key gates) passing locally.
        return "ollama"
    return config.META_API_KEY or os.environ.get("META_API_KEY", "")


def _api_base() -> str:
    if PROVIDER == "ollama":
        return _ollama_base() + "/v1"
    return config.META_API_BASE or os.environ.get("META_API_BASE", "")


def _api_model() -> str:
    if PROVIDER == "ollama":
        return os.environ.get("OLLAMA_MODEL") or "llama3.2:3b"
    return config.META_MODEL or os.environ.get("META_MODEL", "")


def _vision_model() -> str:
    """Vision model for image transcription: the vision model
    under ollama, the chat model under meta (muse-spark reads
    images through the same endpoint)."""
    if PROVIDER == "ollama":
        return os.environ.get("OLLAMA_VISION_MODEL") or "moondream"
    return _api_model()


def has_key() -> bool:
    """True only when META_API_KEY is non-empty after stripping."""
    return bool(_api_key().strip())


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence line (e.g. ``` or ```json).
        newline = stripped.find("\n")
        if newline == -1:
            return ""
        stripped = stripped[newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")]
        return stripped.strip()
    return stripped


def _message_content(data) -> str | None:
    """Message text from a chat-completion payload, or None.

    A 200 response with null content is a real provider shape (seen
    live on the small-talk path: "'NoneType' object has no attribute
    'strip'"). Null, missing, or non-text content degrades to None
    here — callers log it and reply nothing for that turn — instead
    of crashing on .strip(). Never raises.
    """
    try:
        if not isinstance(data, dict):
            return None
        choices = data.get("choices")
        if not choices:
            return None
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        return content if isinstance(content, str) else None
    except Exception:
        return None


_http_client = None


def _get_http_client() -> httpx.Client:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.Client(trust_env=False)
    return _http_client


def chat_json(system: str, user: str, timeout: float = 60.0) -> dict:
    """POST an OpenAI-compatible chat completion and parse JSON content.

    Raises RuntimeError("META_API_KEY not set") when no key is present,
    raises on HTTP errors, and raises ValueError when the model content
    is missing or is not parseable JSON returning a dict object.
    The key and the Authorization header are never logged or printed.
    """
    key = _api_key()
    if not key.strip():
        raise RuntimeError("META_API_KEY not set")
    url = _api_base().rstrip("/") + "/chat/completions"
    payload = {
        "model": _api_model(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
    }
    client = _get_http_client()
    resp = client.post(
        url,
        json=payload,
        headers={
            "Authorization": "Bearer " + key.strip(),
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("model response has no message content") from exc
    if not isinstance(content, str):
        raise ValueError("model response content is not text")
    parsed = json.loads(_strip_code_fences(content))
    if not isinstance(parsed, dict):
        raise ValueError("model response JSON is not an object")
    return parsed


_EXTRACT_SYSTEM = (
    "Extract atomic, self-contained policy facts from the input text.\n"
    "Rules:\n"
    "- Each fact is 1-2 sentences, self-contained, and understandable alone.\n"
    "- Replace pronouns with the names they refer to.\n"
    "- NEVER drop a proper noun, number, date, or title from the input.\n"
    "- NEVER invent a number, date, or name that is not in the input.\n"
    "- If the input states a change, capture the transition in ONE fact: "
    'the new state AND what it replaces (e.g. "Travel limit changed '
    'from 8000 to 12000 rupees"), never just the new state alone.\n'
    "- For each fact, emit visible_to: the roles allowed to see it. "
    'Default to ["employee", "cfo", "cto", "ceo"] (a general fact). '
    "Restrict it only when the input explicitly scopes the fact: "
    'finance facts to ["cfo", "ceo"], technical facts to '
    '["cto", "ceo"].\n'
    '- For each fact, emit effective_date as "YYYY-MM-DD" or null, '
    "only when the input states an effective date.\n"
    "- For each fact, emit triples: a list of objects with keys "
    '"source", "relationship", and "destination", using normalized '
    "lowercase strings with spaces replaced by underscores, for example "
    '{"source": "travel_policy", "relationship": "limit", '
    '"destination": "12000"}.\n'
    "- Respond with JSON only: "
    '{"facts": [{"fact": "...", "visible_to": [...], '
    '"effective_date": null, "triples": [...]}]}.\n'
    "- Do not add any numbering computed at write time by the caller; "
    "just return the facts as described above."
)


def _normalize_triple_value(value) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = "_".join(value.strip().lower().split())
    return normalized or None


def _clean_fact_entry(entry) -> dict | None:
    if not isinstance(entry, dict):
        return None
    fact = entry.get("fact")
    if not isinstance(fact, str) or not fact.strip():
        return None
    visible_to = entry.get("visible_to")
    if isinstance(visible_to, list):
        cleaned_roles = [
            role.strip().lower()
            for role in visible_to
            if isinstance(role, str) and role.strip()
        ]
        if not cleaned_roles:
            cleaned_roles = list(_DEFAULT_VISIBLE_TO)
    else:
        cleaned_roles = list(_DEFAULT_VISIBLE_TO)
    effective_date = entry.get("effective_date")
    if effective_date is not None:
        if not isinstance(effective_date, str) or not _DATE_RE.match(
            effective_date.strip()
        ):
            effective_date = None
        else:
            try:
                datetime.strptime(effective_date.strip(), "%Y-%m-%d")
            except ValueError:
                effective_date = None
            else:
                effective_date = effective_date.strip()
    triples = entry.get("triples")
    cleaned_triples = []
    if isinstance(triples, list):
        for triple in triples:
            if not isinstance(triple, dict):
                continue
            source = _normalize_triple_value(triple.get("source"))
            relationship = _normalize_triple_value(triple.get("relationship"))
            destination = _normalize_triple_value(triple.get("destination"))
            if source is None or relationship is None or destination is None:
                continue
            cleaned_triples.append(
                {
                    "source": source,
                    "relationship": relationship,
                    "destination": destination,
                }
            )
    return {
        "fact": fact.strip(),
        "visible_to": cleaned_roles,
        "effective_date": effective_date,
        "triples": cleaned_triples,
    }


def extract_facts(text: str, company_hint: str = "") -> list[dict]:
    """Extract policy facts from text, or [] when no API key is set.

    An empty return means "no extraction", never an error, when the key
    is missing. Malformed entries in a model response are dropped;
    only clean entries are returned.
    """
    if not has_key():
        return []
    user_parts = []
    if company_hint and company_hint.strip():
        user_parts.append("Company context: " + company_hint.strip() + "\n\n")
    user_parts.append(
        "Extract facts from the following text and return JSON "
        '{"facts": [{fact, visible_to, effective_date, triples}]}:\n\n' + text
    )
    response = chat_json(_EXTRACT_SYSTEM, "".join(user_parts))
    facts = response.get("facts")
    if not isinstance(facts, list):
        return []
    cleaned = []
    for entry in facts:
        clean = _clean_fact_entry(entry)
        if clean is not None:
            cleaned.append(clean)
    return cleaned


_SYNTHESIZE_SYSTEM = (
    "Answer ONLY from the supplied facts. Do not use any other knowledge, "
    "and do not guess.\n"
    "- Cite each fact you use by its id in brackets, like [id: abc12345], "
    "using the short id shown with each fact.\n"
    "- If the supplied facts do not answer the question, set the answer "
    "to the empty string. Do not guess and do not explain.\n"
    "- Keep the answer to 2-4 short sentences, in plain language.\n"
    "- Facts listed under conflicts are conflicting or superseded facts: "
    "do not present them as the current answer. Mention a conflict only "
    "briefly if it is directly relevant to the question.\n"
    '- Respond with JSON only: {"answer": "..."}.'
)


def synthesize(question: str, facts: list, conflict_rows: list) -> str:
    """Answer a question from supplied facts, or '' when impossible.

    Returns '' when there is no API key, when no usable facts are
    supplied, when the model says the facts do not answer the question,
    or when the model response is unparseable or empty.
    """
    if not has_key():
        return ""
    usable = []
    for entry in facts or []:
        if not isinstance(entry, dict):
            continue
        fact_text = entry.get("fact")
        fact_id = entry.get("id")
        if not isinstance(fact_text, str) or not fact_text.strip():
            continue
        if fact_id is None or str(fact_id).strip() == "":
            continue
        usable.append({"id": str(fact_id)[:8], "fact": fact_text.strip()})
    if not usable:
        return ""
    # Anti-hallucination ID remap (mem0 rule): the model never sees real
    # UUIDs. It sees sequential "0", "1", ... and citations are mapped
    # back to the real short ids afterwards, so a hallucinated id can
    # never leak into an answer as if it were a stored fact.
    remap = {str(i): item["id"] for i, item in enumerate(usable)}
    lines = ["Question: " + question, "", "Facts:"]
    for i, item in enumerate(usable):
        lines.append("[id: " + str(i) + "] " + item["fact"])
    conflicts = []
    for row in conflict_rows or []:
        if isinstance(row, dict):
            conflict_text = row.get("fact")
            if isinstance(conflict_text, str) and conflict_text.strip():
                conflicts.append(conflict_text.strip())
    if conflicts:
        lines.append("")
        lines.append("Conflicts:")
        for conflict_text in conflicts:
            lines.append("- " + conflict_text)
    try:
        response = chat_json(_SYNTHESIZE_SYSTEM, "\n".join(lines))
    except Exception:
        return ""
    answer = response.get("answer")
    if not isinstance(answer, str):
        return ""

    # Map remapped citations back to real short ids; an index the model
    # invented (not in remap) is left untouched rather than fabricated.
    def _restore(match):
        return "[id: " + remap.get(match.group(1), match.group(1)) + "]"

    return re.sub(r"\[id:\s*(\d+)\]", _restore, answer.strip())


def extract_and_store(
    conn, company_id, text, source, source_message_id=None, document_id=None
) -> list[str]:
    """Extract facts from text and store each one; return stored ids.

    Never raises into a request path: on any exception, return the ids
    collected so far (empty when extraction itself fails).
    """
    try:
        facts = extract_facts(text)
    except Exception:
        return []
    ids: list[str] = []
    if not facts:
        return ids
    try:
        from . import store
    except ImportError:  # pragma: no cover - non-package import fallback
        try:
            import store  # type: ignore[no-redef]
        except ImportError:
            return ids
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        fact_text = fact.get("fact")
        if not isinstance(fact_text, str) or not fact_text.strip():
            continue
        # RLS scope restore: this function runs only on intake
        # connections (WhatsApp media, /upload), which the caller
        # opens operator-scoped (ceo + unlocked). Each write_fact
        # commits, and a commit clears SET LOCAL variables — so the
        # scope must be re-applied before every fact, or the second
        # restricted-scope fact would fail its INSERT ... RETURNING
        # check. Best-effort: general rows write fine without it.
        try:
            conn.execute(
                "SELECT set_config('app.current_role', 'ceo', true)"
            )
            conn.execute(
                "SELECT set_config('app.sensitive_unlocked', 'on', true)"
            )
        except Exception:
            pass
        try:
            stored_id = store.write_fact(
                conn,
                company_id,
                fact=fact_text,
                visible_to=fact.get("visible_to")
                or ["employee", "cfo", "cto", "ceo"],
                effective_date=fact.get("effective_date"),
                source=source,
                source_message_id=source_message_id,
                document_id=document_id,
                triples=fact.get("triples"),
            )
        except Exception:
            return ids
        ids.append(stored_id)
    return ids


def generate_answer(message: str, context_facts: list | None = None) -> str:
    """Send user message directly to Meta Model API with relevant context and return pure raw response.

    No hardcoded greetings, no canned fallback messages, no preset answers.
    Whatever the user sends, Meta Model API answers directly and naturally.
    """
    key = _api_key().strip()
    if not key:
        return ""

    if not context_facts:
        return ""

    system_parts = [
        "You are an assistant answering questions strictly from the company's uploaded documents.",
        "Answer ONLY based on the facts provided below. Do NOT use general knowledge or guess.",
        "Keep the answer concise and direct (1-2 short sentences, at most 3 lines).",
        "\nRelevant company documents and records:",
    ]
    for fact in context_facts:
        src = fact.get("doc_title") or fact.get("source") or "document"
        text = fact.get("fact", "")
        eff = fact.get("effective_date")
        date_str = f" (effective {eff})" if eff else ""
        system_parts.append(f"- [{src}{date_str}]: {text}")
    system_parts.append(
        "\nIf the facts do not answer the question, reply with an empty string."
    )

    system = "\n".join(system_parts)
    url = _api_base().rstrip("/") + "/chat/completions"
    try:
        resp = httpx.post(
            url,
            json={
                "model": _api_model(),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": message},
                ],
                "max_tokens": 4096,
                "temperature": 0.7,
            },
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
            trust_env=False,
        )
        resp.raise_for_status()
        data = resp.json()
        content = _message_content(data)
        if not content or not content.strip():
            import logging

            logging.getLogger(__name__).warning(
                "generate_answer returned no content; replying nothing"
            )
            return ""
        return content.strip()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("generate_answer error: %s", exc)
        return ""


def generate_doc_reply(filename: str, text: str | None, fact_count: int) -> str:
    """Generate a dynamic acknowledgment from Meta Model API when a document is uploaded."""
    key = _api_key().strip()
    if not key:
        return ""
    if text:
        prompt = (
            f"The user just uploaded a document named '{filename}'. "
            f"Here is a brief snippet of its content:\n{text[:600]}\n\n"
            f"Acknowledge receiving and reading this document naturally in 1-2 friendly sentences. "
            f"Mention what the document covers and let the user know they can ask any questions about it."
        )
    else:
        prompt = (
            f"The user just sent a file named '{filename}', but text could not be extracted from it. "
            f"Inform the user naturally and politely in 1 sentence."
        )
    url = _api_base().rstrip("/") + "/chat/completions"
    try:
        resp = httpx.post(
            url,
            json={
                "model": _api_model(),
                "messages": [
                    {"role": "system", "content": "You are a helpful WhatsApp AI assistant."},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 2048,
                "temperature": 0.7,
            },
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
            trust_env=False,
        )
        resp.raise_for_status()
        data = resp.json()
        content = _message_content(data)
        if not content or not content.strip():
            import logging

            logging.getLogger(__name__).warning(
                "generate_doc_reply returned no content; replying nothing"
            )
            return ""
        return content.strip()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("generate_doc_reply error: %s", exc)
        return ""


def reply_conversation(message: str) -> str:
    return generate_answer(message)


def generate_smalltalk(message: str) -> str:
    """Brief capabilities intro using configured agent name."""
    return config.intro_message()


def compose_email(target_display: str, topic: str, facts: list) -> str:
    """Draft a workplace email from engine facts ONLY, or "".

    The model receives the covering facts and nothing else, and is
    instructed to use only them: no invented figures, names, dates,
    or policy details. Returns "" when no key is set, the call fails,
    or the model returns no content — the caller then falls back to
    the deterministic facts rendering, so an email is never composed
    from anything but facts. Same null-content guard discipline as
    every other LLM path in this module.
    """
    key = _api_key().strip()
    if not key:
        return ""
    usable = []
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        text = fact.get("fact")
        if isinstance(text, str) and text.strip():
            usable.append(text.strip())
    if not usable:
        return ""
    fact_block = "\n".join(f"- {text}" for text in usable)
    url = _api_base().rstrip("/") + "/chat/completions"
    try:
        resp = httpx.post(
            url,
            json={
                "model": _api_model(),
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are drafting a professional workplace "
                            "email. Use ONLY the facts listed below. Do "
                            "not invent any figure, name, date, or policy "
                            "detail, and do not use any other knowledge. "
                            "If a detail is not in the facts, leave it "
                            "out. Start with a subject line, then the "
                            "email body."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Write an email to {target_display} about "
                            f"{topic}, using only these facts:\n{fact_block}"
                        ),
                    },
                ],
                "max_tokens": 2048,
                "temperature": 0.2,
            },
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
            },
            timeout=30.0,
            trust_env=False,
        )
        resp.raise_for_status()
        data = resp.json()
        content = _message_content(data)
        if not content or not content.strip():
            import logging

            logging.getLogger(__name__).warning(
                "compose_email returned no content; caller falls back"
            )
            return ""
        return content.strip()
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning("compose_email error: %s", exc)
        return ""


def transcribe_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> str | None:
    """Extract and transcribe all text, numbers, and information
    from image bytes, via the selected provider (vision model
    under ollama, chat model under meta)."""
    import base64
    key = _api_key().strip()
    if not key or not image_bytes:
        return None

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{b64}"
    url = _api_base().rstrip("/") + "/chat/completions"

    payload = {
        "model": _vision_model(),
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Please read and transcribe all visible text, numbers, dates, titles, and details "
                            "from this image accurately and completely. Preserve headings and layout structure. "
                            "Output only the extracted transcription without chat preamble."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            }
        ],
        "max_tokens": 1500,
        "temperature": 0.2,
    }

    try:
        resp = httpx.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
            trust_env=False,
        )
        resp.raise_for_status()
        data = resp.json()
        content = _message_content(data)
        if not content or not content.strip():
            import logging

            logging.getLogger(__name__).warning(
                "transcribe_image returned no content"
            )
            return None
        return content.strip()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("transcribe_image error: %s", exc)
        return None
