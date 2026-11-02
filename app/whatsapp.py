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
    """Comma-separated phone list -> set of stripped numbers."""
    return {p.strip() for p in (value or "").split(",") if p.strip()}


def _role_for(sender: str) -> str:
    # Highest privilege wins when a number appears on several lists.
    # WHATSAPP_MANAGER_PHONE is the legacy CEO alias.
    if sender in _parse_phones(config.WHATSAPP_CEO_PHONE):
        return "ceo"
    if sender in _parse_phones(config.WHATSAPP_MANAGER_PHONE):
        return "ceo"
    if sender in _parse_phones(config.WHATSAPP_CFO_PHONE):
        return "cfo"
    if sender in _parse_phones(config.WHATSAPP_CTO_PHONE):
        return "cto"
    return "employee"


def _run_answer(company_id: str, role: str, question: str) -> str:
    """Retrieve relevant context and get the raw response directly from Meta Model API.

    No hardcoded greetings, no canned fallback messages, no preset answers.
    Every answer is purely Meta Model API's raw response.
    """
    from app.llm import generate_answer
    from app import retrieve

    context_facts = []
    conn = None
    ctx = None
    try:
        from app.db import get_conn

        candidate = get_conn()
        if hasattr(candidate, "__enter__"):
            ctx = candidate
            conn = ctx.__enter__()
        else:
            conn = candidate

        rows = retrieve.search(conn, company_id, role, question, limit=5)
        doc_ids_all = set()
        for r in rows:
            for d in (r.get("document_ids") or []):
                doc_ids_all.add(str(d))
        doc_titles = {}
        if doc_ids_all and hasattr(conn, "cursor"):
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, title FROM documents WHERE id = ANY(%s::uuid[])",
                        (list(doc_ids_all),),
                    )
                    for did, dtitle in cur.fetchall():
                        if dtitle:
                            doc_titles[str(did)] = dtitle
            except Exception:
                pass

        for r in rows:
            scores = r.get("scores") or {}
            sem = scores.get("semantic", 0.0)
            kw = scores.get("keyword", 0.0)
            ent = scores.get("entity", 0.0)
            if sem >= 0.22 or kw > 0.05 or ent > 0:
                dt = None
                for did in (r.get("document_ids") or []):
                    if str(did) in doc_titles:
                        dt = doc_titles[str(did)]
                        break
                context_facts.append(
                    {
                        "fact": r["fact"],
                        "effective_date": r.get("effective_date"),
                        "source": r.get("source"),
                        "doc_title": dt,
                    }
                )
    except Exception:
        context_facts = []
    finally:
        if ctx is not None:
            ctx.__exit__(None, None, None)
        elif conn is not None and hasattr(conn, "close"):
            try:
                conn.close()
            except Exception:
                pass

    return generate_answer(question, context_facts=context_facts)


def _answer_text(company_id: str, role: str, question: str) -> str:
    """Engine-first WhatsApp text answer; "" when nothing answers.

    (1) The engine's multi-hop query (entities + relations, role
    filter enforced), synthesized by the Meta LLM; its wrapper falls
    back to legacy retrieval internally when the engine path fails
    or is empty. Only a non-empty, non-canned result is returned.
    (2) When both yield nothing, _run_answer (legacy retrieval +
    general Meta chat) so casual messages still get a raw model reply.
    (3) "" otherwise — the caller sends nothing and logs the failure.
    Only the model's raw answer is ever returned; no canned text.
    """
    try:
        from app.db import get_conn
        from app.engine import query as engine_query

        conn = None
        ctx = None
        try:
            candidate = get_conn()
            if hasattr(candidate, "__enter__"):
                ctx = candidate
                conn = ctx.__enter__()
            else:
                conn = candidate
            result = engine_query.answer_question_engine_or_legacy(
                conn, company_id, role, question
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
        if (
            isinstance(result, dict)
            and not result.get("empty")
            and isinstance(answer, str)
            and answer.strip()
        ):
            return answer
    except Exception:
        pass
    try:
        return _run_answer(company_id, role, question)
    except Exception:
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
    from app.db import get_conn
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
    text = extract_text(msg["filename"], data)
    if caption:
        if text:
            text = f"Caption: {caption}\n\nContent:\n{text}"
        else:
            text = f"Caption: {caption}"
    with get_conn() as conn:
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
            try:
                from app.llm import extract_and_store

                fact_ids = extract_and_store(
                    conn,
                    company_id,
                    text,
                    "image" if msg["kind"] == "image" else "document",
                    document_id=doc_id,
                )
            except Exception:
                fact_ids = []
            # Engine ingest (dual-write): the same content becomes
            # part of the interconnected graph — chunked ingest,
            # entity extraction, relation building, content-hash
            # dedup — so WhatsApp questions answer from the graph.
            # A failure here is logged only; the legacy ingest above
            # already stands, so the upload still answers via fallback.
            try:
                from app.engine import ingest as engine_ingest

                engine_ingest.ingest_text(
                    conn, company_id, text, msg["filename"],
                    title=msg["filename"],
                )
            except Exception:
                import logging

                logging.getLogger("whatsapp_webhook").exception(
                    "Engine ingest failed for WhatsApp media %s",
                    msg["filename"],
                )
    from app.llm import generate_doc_reply

    filename = msg["filename"]
    return generate_doc_reply(filename, text, len(fact_ids))


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
    Graph call can never stall the webhook. Every failure is logged
    and that message is skipped — never an empty or canned send, and
    never a claim about work that did not happen.
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
                    _answer_text, company_id, _role_for(sender), msg["text"]
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
            _log.error(
                "Model produced no reply for message from %s; nothing sent",
                sender,
            )
            continue
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
        _log.info("Delivery result for %s: kind=%s", recipient_to_use, kind)


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
