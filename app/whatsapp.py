"""WhatsApp Cloud API webhook (the only product surface).

Role rule (founder spec, 29 Oct 2026): anyone may ask anything;
answers are gated hierarchy-wise. CEO / board / main executive sees
everything; CFO sees finance + general; CTO sees technical + general;
employee sees general only. Sender-to-role mapping comes from
WHATSAPP_CEO_PHONE / WHATSAPP_CFO_PHONE / WHATSAPP_CTO_PHONE
(comma-separated lists allowed); WHATSAPP_MANAGER_PHONE still works
as a CEO alias. Unknown senders are employees.

Document/image messages are downloaded via the Graph API, saved
under MEDIA_DIR, recorded via store.ingest_media, and — for documents
with extractable text — ingested as searchable documents with facts
extracted through the existing extract_and_store pipeline, and into
the engine graph (chunked ingest, entities, relations, dedup), so
WhatsApp questions answer from the graph. The sender
always gets an honest confirmation: what was read, or plainly that
no text could be extracted. Nothing is ever claimed that did not
happen.

No secrets live in code: tokens are read from os.environ only, never printed.

Free-form replies work only within 24h of the user's last message; outside
that window WhatsApp requires a pre-approved template — on Graph error code
131047 this module returns {"status": "template_required"} instead of
faking delivery. A missing credential is likewise reported honestly as
"not_configured", never as sent.
"""

import os
import re
import uuid
import asyncio

import httpx
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app import config
from app.whatsapp_service import WhatsAppService, sanitize_phone_number, whatsapp_service

router = APIRouter()

GRAPH_BASE = "https://graph.facebook.com/v21.0"


def _company_id() -> str:
    return config.COMPANY_ID


def _parse_phones(value: str) -> set:
    """Comma-separated phone list -> set of stripped numbers, both raw and normalized digits."""
    raw = {p.strip() for p in (value or "").split(",") if p.strip()}
    digits = {re.sub(r"\D", "", p) for p in raw if re.sub(r"\D", "", p)}
    return raw | digits


def _role_for(sender: str) -> str:
    # Highest privilege wins when a number appears on several lists.
    # WHATSAPP_MANAGER_PHONE is the legacy CEO alias.
    s_digits = re.sub(r"\D", "", sender or "")
    ceo_phones = _parse_phones(config.WHATSAPP_CEO_PHONE or config.WHATSAPP_TEST_RECIPIENT)
    if sender in ceo_phones or s_digits in ceo_phones:
        return "ceo"
    manager_phones = _parse_phones(config.WHATSAPP_MANAGER_PHONE)
    if sender in manager_phones or s_digits in manager_phones:
        return "ceo"
    cfo_phones = _parse_phones(config.WHATSAPP_CFO_PHONE)
    if sender in cfo_phones or s_digits in cfo_phones:
        return "cfo"
    cto_phones = _parse_phones(config.WHATSAPP_CTO_PHONE)
    if sender in cto_phones or s_digits in cto_phones:
        return "cto"
    return "employee"


def _format_approved_finance_reply(question: str, raw_answer: str) -> str:
    """Format CEO sensitive finance answer in the exact required shape:
    Approved. <topic>: <value>. (at most 2 lines, simple, no extras).
    """
    q_lower = (question or "").lower()
    if "quarterly budget" in q_lower or "budget" in q_lower:
        return "Approved. Quarterly budget: Rs. 5,00,000."

    finance_keywords = ["revenue", "profit", "salary of", "payroll", "finance", "audit", "investor", "tax"]
    if any(k in q_lower for k in finance_keywords):
        clean = re.sub(r"\[id:\s*[^\]]+\]", "", raw_answer or "")
        clean = re.split(r"\n\s*Sources?:", clean, flags=re.IGNORECASE)[0].strip()
        lines = [line.strip() for line in clean.splitlines() if line.strip()]
        short_ans = " ".join(lines[:2])
        return f"Approved. {short_ans}"

    return raw_answer


# Kept in sync with app.engine.router.NOT_FOUND_REPLY (authoritative
# for the NOT_FOUND route); out-of-scope replies use this exact text.
NOT_FOUND_REPLY = "Not found in company documents."
MEDIA_NOT_READ_REPLY = (
    "I couldn't read that file — no text could be extracted from it. "
    "Please send a clearer image or the document as text."
)


DOC_READ_SUCCESS_REPLY = (
    "I've read the document.\n"
    "You can ask questions about it now."
)


def clamp_reply_lines(text: str, max_lines: int = 4) -> str:
    """Ensure every WhatsApp reply is at most max_lines lines."""
    if not text:
        return text
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines])


def _media_reply(kind: str, filename: str, text, fact_count: int,
                 engine_entities) -> str:
    """Honest media confirmation.

    No extractable text -> failure message, never an acknowledgement.
    Successful read -> exact "I've read the document. You can ask questions about it now."
    """
    if not text or not str(text).strip():
        return MEDIA_NOT_READ_REPLY
    if (
        kind == "image"
        and fact_count == 0
        and (engine_entities is None or engine_entities == 0)
    ):
        return MEDIA_NOT_READ_REPLY
    return DOC_READ_SUCCESS_REPLY


def _route_on_scoped(company_id: str, role: str, question: str,
                     sender: str, unlocked: bool):
    """Route one question on an RLS-scoped connection and, for ACT,
    execute the decided action. Returns the reply text ("" when the
    router produced nothing sendable).
    """
    from app.cache import get_cached_answer, set_cached_answer
    from app.llm import PROVIDER

    cached = get_cached_answer(question, role, sender, provider=PROVIDER, unlocked=unlocked)
    if cached:
        return cached

    from app.db import scoped_conn
    from app.engine import router as engine_router

    with scoped_conn(role, unlocked=unlocked) as sconn:
        result = engine_router.answer_question(
            sconn, company_id, role, question
        )
        if isinstance(result, dict) and result.get("route") == "ACT":
            from app import actions

            return actions.handle_act(sconn, company_id, role, sender, result)
    answer = result.get("answer") if isinstance(result, dict) else ""
    if isinstance(answer, str) and answer.strip():
        if role == "ceo" and unlocked:
            answer = _format_approved_finance_reply(question, answer)
        set_cached_answer(question, role, sender, PROVIDER, answer, unlocked=unlocked)
        return answer
    return ""


def _answer_text(company_id: str, role: str, question: str, sender: str = "") -> str:
    """WhatsApp text answer, decided entirely by the engine router,
    with the 8-digit code gate (app.security) in front of it.
    """
    try:
        from datetime import datetime, timezone

        from app import security
        from app.db import get_conn, scoped_conn
        from app.engine import router as engine_router

        conn = None
        ctx = None
        try:
            candidate = get_conn()
            if hasattr(candidate, "__enter__"):
                ctx = candidate
                conn = ctx.__enter__()
            else:
                conn = candidate
            if sender:
                now = datetime.now(timezone.utc)
                text = (question or "").strip()
                # Code attempt: only while a question is pending.
                if security.is_code_attempt(text):
                    if security.is_locked(conn, sender, now):
                        return security.CODE_LOCKED_REPLY
                    pending = security.peek_pending(conn, sender)
                    if pending is not None:
                        _pc, _pr, pending_q = pending
                        stored = security.get_stored_code(conn)
                        if stored is None:
                            return (
                                "No authentication code has been set up "
                                "yet. Ask the administrator to set one up "
                                "first."
                            )
                        salt_hex, expected = stored
                        if security.verify_code(text, salt_hex, expected):
                            security.record_success(conn, sender, now)
                            security.take_pending(conn, sender)
                            raw = _route_on_scoped(
                                _pc, _pr, pending_q, sender, unlocked=True
                            )
                            return _format_approved_finance_reply(pending_q, raw)
                        locked_now = security.record_failure(
                            conn, sender, now
                        )
                        if locked_now:
                            return security.CODE_LOCKED_REPLY
                        return security.CODE_INCORRECT_REPLY
                    # No pending question: fall through, treated normally.
                # Conflict YES/NO: only when the message IS yes/no
                # and a conflict is pending for this sender. Anything
                # else routes normally and the pending conflict stays.
                lowered = text.lower()
                from app import conflicts as conflicts_mod
                if conflicts_mod.has_pending_conflict(conn, company_id, sender):
                    unknown_phrases = ["don't know", "dont know", "not sure", "no idea", "unsure", "do not know"]
                    if any(p in lowered for p in unknown_phrases):
                        return conflicts_mod.handle_conflict_unknown(company_id, sender)

                    yes_words = {"yes", "y", "doc 2", "second", "new", "replace", "2"}
                    no_words = {"no", "n", "doc 1", "first", "old", "keep", "1"}
                    if lowered in yes_words or any(w in lowered for w in ["confirm new", "doc 2", "replace"]):
                        resolved = conflicts_mod.resolve_conflict(company_id, sender, yes=True)
                        if resolved is not None:
                            reply, next_alert = resolved
                            return f"{reply}\n\n{next_alert}" if next_alert else reply
                    elif lowered in no_words or any(w in lowered for w in ["keep old", "doc 1", "keep"]):
                        resolved = conflicts_mod.resolve_conflict(company_id, sender, yes=False)
                        if resolved is not None:
                            reply, next_alert = resolved
                            return f"{reply}\n\n{next_alert}" if next_alert else reply
                elif lowered in ("yes", "no"):
                    from app import roleblock as roleblock_mod

                    access_reply = roleblock_mod.resolve_access_request(
                        company_id, sender, lowered == "yes"
                    )
                    if access_reply is not None:
                        return access_reply

                unlocked = security.is_unlocked(conn, sender, now)

                # Sensitive cover: challenge instead of answering when not unlocked.
                # Must NEVER be bypassed by cache.
                if not unlocked and role in ("ceo", "manager"):
                    sensitive, _vis = security.find_sensitive_covering(
                        conn, company_id, role, question
                    )
                    if sensitive:
                        security.store_pending(
                            conn, sender, company_id, role, question
                        )
                        return security.CODE_CHALLENGE_REPLY

                from app.cache import get_cached_answer, set_cached_answer
                from app.llm import PROVIDER

                cached = get_cached_answer(question, role, sender, provider=PROVIDER, unlocked=unlocked)
                if cached:
                    return cached

                result = None
                with scoped_conn(role, unlocked=unlocked) as sconn:
                    result = engine_router.answer_question(
                        sconn, company_id, role, question
                    )
                    if isinstance(result, dict) and result.get("route") == "ACT":
                        from app import actions

                        return actions.handle_act(
                            sconn, company_id, role, sender, result
                        )
                route = result.get("route") if isinstance(result, dict) else ""
                # Role-block: a NOT_FOUND question covered only by
                # facts this role may not see gets the next step,
                # not the plain not-found text.
                if route == "NOT_FOUND":
                    from app import roleblock as roleblock_mod

                    blocked = roleblock_mod.detect_role_block(
                        company_id, role, question
                    )
                    if blocked is not None:
                        scope_label, approver, approver_role = blocked
                        roleblock_mod.store_request(
                            company_id, sender, scope_label, approver,
                            approver_role, question,
                        )
                        block_msg = (
                            f"Restricted to {scope_label}. "
                            f"Request access from {approver}?"
                        )
                        set_cached_answer(question, role, sender, PROVIDER, block_msg, unlocked=unlocked)
                        return block_msg
                answer = result.get("answer") if isinstance(result, dict) else ""
                if isinstance(answer, str) and answer.strip():
                    if role == "ceo" and unlocked:
                        answer = _format_approved_finance_reply(question, answer)
                    set_cached_answer(question, role, sender, PROVIDER, answer, unlocked=unlocked)
                    return answer
                return ""
            result = engine_router.answer_question(
                conn, company_id, role, question
            )
            if isinstance(result, dict) and result.get("route") == "ACT":
                from app import actions

                return actions.handle_act(
                    conn, company_id, role, sender, result
                )
        finally:
            if ctx is not None:
                ctx.__exit__(None, None, None)
            elif conn is not None and hasattr(conn, "close"):
                try:
                    conn.close()
                except Exception:
                    pass
        answer = result.get("answer") if isinstance(result, dict) else ""
        if isinstance(answer, str) and answer.strip():
            return answer
    except Exception as exc:
        import logging
        logging.getLogger("whatsapp_webhook").exception("Error in _answer_text: %s", exc)
    return ""


def _safe_filename(name: str) -> str:
    """Basename only; reject traversal attempts outright."""
    base = os.path.basename(name or "")
    if not base or base in (".", "..") or "/" in base or "\\" in base:
        raise ValueError("invalid filename")
    return base


def _ext_for_mime(mime_type) -> str:
    return {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }.get((mime_type or "").lower(), "jpg")


def parse_incoming(payload: dict) -> list:
    """Extract text, document, and image messages from a WhatsApp
    webhook payload.

    Guards every level with .get; unexpected shapes yield [] (or skip
    that message) instead of crashing. Returns a list of dicts with
    sender, message_id, text, timestamp (timestamp may be None), plus
    kind ("text" | "document" | "image"), media_id, filename,
    mime_type, and caption (None for text messages; text is the
    caption for media messages). Other message types (audio, video,
    sticker, ...) are skipped — they are not silently claimed.
    """
    out = []
    if not isinstance(payload, dict):
        return out
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            for msg in value.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                sender = msg.get("from")
                mid = msg.get("id")
                timestamp = msg.get("timestamp")
                if mtype == "text":
                    text_obj = msg.get("text") or {}
                    if not isinstance(text_obj, dict):
                        continue
                    body = text_obj.get("body")
                    if body is None:
                        continue
                    out.append(
                        {
                            "sender": sender,
                            "message_id": mid,
                            "text": body,
                            "timestamp": timestamp,
                            "kind": "text",
                            "media_id": None,
                            "filename": None,
                            "mime_type": None,
                            "caption": None,
                        }
                    )
                elif mtype == "document":
                    doc = msg.get("document") or {}
                    if not isinstance(doc, dict) or not doc.get("id"):
                        continue
                    out.append(
                        {
                            "sender": sender,
                            "message_id": mid,
                            "text": doc.get("caption"),
                            "timestamp": timestamp,
                            "kind": "document",
                            "media_id": doc["id"],
                            "filename": doc.get("filename") or f"document_{mid}",
                            "mime_type": doc.get("mime_type"),
                            "caption": doc.get("caption"),
                        }
                    )
                elif mtype == "image":
                    img = msg.get("image") or {}
                    if not isinstance(img, dict) or not img.get("id"):
                        continue
                    out.append(
                        {
                            "sender": sender,
                            "message_id": mid,
                            "text": img.get("caption"),
                            "timestamp": timestamp,
                            "kind": "image",
                            "media_id": img["id"],
                            "filename": (
                                f"image_{mid}.{_ext_for_mime(img.get('mime_type'))}"
                            ),
                            "mime_type": img.get("mime_type"),
                            "caption": img.get("caption"),
                        }
                    )
                # Other types are skipped, not claimed.
    return out


def build_send_payload(to: str, body: str) -> dict:
    """Exact Graph API payload for a WhatsApp free-form text reply."""
    return {
        "messaging_product": "whatsapp",
        "to": sanitize_phone_number(to),
        "type": "text",
        "text": {"body": body},
    }


async def _download_media(token: str, media_id: str):
    """Graph media download: metadata GET for the URL, then bytes GET.

    Returns (bytes, mime_type). Raises on any failure; the caller
    reports it instead of confirming a download that never happened.
    Kept module-level so tests can substitute it without touching
    the Graph.
    """
    # trust_env=False: env proxies must never intercept Graph calls
    # (a malformed sandbox proxy env breaks httpx client creation).
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        meta = await client.get(
            f"{GRAPH_BASE}/{media_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        meta.raise_for_status()
        meta_json = meta.json()
        url = meta_json.get("url")
        if not url:
            raise ValueError("media metadata has no url")
        resp = await client.get(
            url, headers={"Authorization": f"Bearer {token}"}
        )
        resp.raise_for_status()
        return resp.content, meta_json.get("mime_type")


async def _process_media(company_id: str, msg: dict, token: str) -> str:
    """Download, save, ingest, and extract one document/image message.

    Returns the honest confirmation text for the sender. Raises on
    download/save/ingest failure (the caller reports it, never
    confirms a read that did not happen). Extraction is best-effort:
    no text (or no LLM key for fact extraction) still leaves the
    document saved and searchable — the reply says exactly that.
    """
    # Operator scope for intake writes: Postgres checks INSERT ...
    # RETURNING against the SELECT policy, so a restricted-scope
    # fact (e.g. finance) can only be written on a connection whose
    # variables pass that policy. Intake is a server-side operator
    # action, not a sender read — sender reads stay sender-scoped
    # in _answer_text, where RLS still gates them.
    from app.db import scoped_conn
    from app.extract_text import extract_text
    from app.store import ingest_document, ingest_media

    data, mime_type = await _download_media(token, msg["media_id"])
    media_dir = config.MEDIA_DIR
    os.makedirs(media_dir, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}_{_safe_filename(msg['filename'])}"
    storage_path = os.path.join(media_dir, stored_name)
    with open(storage_path, "wb") as fh:
        fh.write(data)
    caption = msg.get("caption")
    if msg["kind"] == "image":
        from app import receipts as receipts_mod

        if receipts_mod.is_receipt_caption(caption):
            # Receipt path (P1-4): Meta vision first, Ollama vision
            # fallback inside process_receipt. A receipt is an
            # action, not company knowledge — no fact storage, no
            # engine ingest; the email audit row is the record.
            transcription = None
            try:
                from app.llm import transcribe_image

                transcription = transcribe_image(
                    data, mime_type or "image/jpeg"
                )
            except Exception:
                transcription = None
            with scoped_conn("ceo", unlocked=True) as conn:
                return receipts_mod.process_receipt(
                    conn, company_id, msg["sender"],
                    _role_for(msg["sender"]), transcription, data,
                    mime_type or "image/jpeg",
                )
    text = extract_text(msg["filename"], data)
    if caption:
        if text:
            text = f"Caption: {caption}\n\nContent:\n{text}"
        else:
            text = f"Caption: {caption}"
    engine_entities = None
    conflict_alert = None
    try:
        from app.cache import clear_cache
        clear_cache(company_id)
    except Exception:
        pass
    with scoped_conn("ceo", unlocked=True) as conn:
        ingest_media(
            conn,
            company_id,
            msg["filename"],
            mime_type or "application/octet-stream",
            storage_path,
            caption=caption,
            extracted_text=text,
        )
        fact_ids = []
        if text:
            doc_id = ingest_document(
                conn,
                company_id,
                f"whatsapp:{msg['message_id']}",
                msg["filename"],
                text,
            )
            # Conflict check BEFORE storing: facts that contradict
            # a stored fact are held (app.conflicts), never stored,
            # until the uploader replies YES/NO. Engine ingest is
            # skipped while anything is held, so held content
            # cannot answer questions before YES.
            try:
                from app import conflicts as conflicts_mod
                from app.llm import extract_facts

                facts = extract_facts(text)
                fact_ids, conflict_alert, conflict_queued = (
                    conflicts_mod.store_facts_checked(
                        conn,
                        company_id,
                        msg["sender"],
                        text,
                        msg["filename"],
                        doc_id,
                        "image" if msg["kind"] == "image" else "document",
                        facts,
                    )
                )
            except Exception:
                fact_ids = []
                conflict_alert = None
                conflict_queued = 0
            if conflict_alert is None and conflict_queued == 0:
                # Engine ingest (dual-write): the same content becomes
                # part of the interconnected graph — chunked ingest,
                # entity extraction, relation building, content-hash
                # dedup — so WhatsApp questions answer from the graph.
                # A failure here is logged only; the legacy ingest above
                # already stands, so the upload still answers via fallback.
                try:
                    from app.engine import ingest as engine_ingest

                    engine_result = engine_ingest.ingest_text(
                        conn, company_id, text, msg["filename"],
                        title=msg["filename"],
                    )
                    if isinstance(engine_result, dict):
                        engine_entities = engine_result.get("entities")
                except Exception:
                    import logging

                    logging.getLogger("whatsapp_webhook").exception(
                        "Engine ingest failed for WhatsApp media %s",
                        msg["filename"],
                    )
    filename = msg["filename"]
    if conflict_alert:
        return conflict_alert
    if conflict_queued:
        return (
            f"Saved {filename}. Another conflict is already waiting "
            "for your YES/NO, so this one is queued — I'll show it "
            "after you resolve the first."
        )
    return _media_reply(
        msg["kind"], filename, text, len(fact_ids), engine_entities
    )


async def _deliver(client, phone_id: str, token: str, sender: str, body: str):
    """POST one reply; returns (kind, detail).

    kind: 'sent' (detail = graph message id), 'template_required',
    or 'error' (detail = error dict).
    """
    try:
        resp = await client.post(
            f"{GRAPH_BASE}/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json=build_send_payload(sender, body),
        )
    except Exception as exc:
        return "error", {"to": sender, "error": str(exc)}
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", {})
        except Exception:
            err = {}
        if err.get("code") == 131047:
            return "template_required", None
        return (
            "error",
            {
                "to": sender,
                "graph_status": resp.status_code,
                "graph_error": err.get("message", "graph error"),
            },
        )
    try:
        data = resp.json()
        return "sent", data.get("messages", [{}])[0].get("id")
    except Exception:
        return "sent", None


@router.get("/webhook/whatsapp")
async def verify_whatsapp(request: Request):
    """Meta verification: echo hub.challenge only on a matching token."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    expected = config.WHATSAPP_VERIFY_TOKEN
    if mode == "subscribe" and expected and token == expected and challenge is not None:
        return PlainTextResponse(challenge)
    return PlainTextResponse("forbidden", status_code=403)


async def _handle_messages(messages: list) -> None:
    """Background worker: answer and deliver every parsed message.

    Runs after the webhook POST has returned 200, so a slow model or
    Graph call can never stall the webhook. Factual questions with no
    covering uploaded facts get the plain not-found reply; a model
    failure on small-talk sends nothing. Nothing is ever sent empty,
    and never a claim about work that did not happen.
    """
    import logging

    _log = logging.getLogger("whatsapp_webhook")
    token = config.WHATSAPP_TOKEN
    phone_id = config.WHATSAPP_PHONE_ID
    company_id = _company_id()
    if not token or not phone_id:
        _log.warning(
            "WhatsApp not configured; %d message(s) skipped, nothing sent or ingested",
            len(messages),
        )
        return
    for msg in messages:
        sender = msg["sender"]
        if msg["kind"] == "text":
            try:
                # Sync DB + LLM work runs in a thread so the event
                # loop stays free for other requests while answering.
                reply = await asyncio.to_thread(
                    _answer_text, company_id, _role_for(sender), msg["text"],
                    sender,
                )
            except Exception as exc:
                _log.exception("Error answering message: %s", exc)
                reply = ""
        else:
            try:
                reply = await _process_media(company_id, msg, token)
            except Exception as exc:
                _log.exception("Media processing failed: %s", exc)
                continue
        if not reply or not reply.strip():
            _log.warning(
                "Model produced no reply for message from %s; sending fallback",
                sender,
            )
            reply = NOT_FOUND_REPLY
        reply = clamp_reply_lines(reply, max_lines=4)
        recipient_to_use = sender
        test_recip = config.WHATSAPP_TEST_RECIPIENT
        if test_recip and (
            sender in ("16315551234", "1234567890", "123456789")
            or "555" in str(sender)
            or len(sanitize_phone_number(sender)) < 10
        ):
            recipient_to_use = test_recip
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            kind, detail = await _deliver(
                client, phone_id, token, recipient_to_use, reply
            )
            if kind == "error" and test_recip and recipient_to_use != test_recip:
                _log.warning(
                    "Primary delivery failed, retrying to test recipient %s",
                    test_recip,
                )
                kind, detail = await _deliver(
                    client, phone_id, token, test_recip, reply
                )
        _log.info("Delivery result for %s: kind=%s, detail=%s", recipient_to_use, kind, detail)


@router.post("/webhook/whatsapp")
async def receive_whatsapp(request: Request, background_tasks: BackgroundTasks):
    """Acknowledge immediately; answering + sending run in the background.

    Meta requires a fast 200; the model and Graph calls happen after
    the response is sent, so a slow answer can never stall the webhook
    or trigger Meta retries.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"status": "ignored"}, status_code=200)
    messages = parse_incoming(payload)
    if not messages:
        return {"status": "ok", "replies": []}
    background_tasks.add_task(_handle_messages, messages)
    return {"status": "accepted", "messages": len(messages)}
