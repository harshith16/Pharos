"""File/image upload endpoint.

Saves uploads locally (MEDIA_DIR, default ./media) and records them via
store.ingest_media. Images are embedded via caption/text only:
embeddinggemma v1 is text-only, so embedded=true appears only when a
vector was actually stored from caption/extracted text. Never claim
pixels were embedded.

Logs record ids and outcomes only — never file contents, tokens, or
message bodies.
"""

import logging
import os
import uuid

from fastapi import APIRouter, File, Form, UploadFile

logger = logging.getLogger(__name__)
router = APIRouter()


def _safe_filename(name: str) -> str:
    """Basename only; reject traversal attempts outright."""
    base = os.path.basename(name or "")
    if not base or base in (".", "..") or "/" in base or "\\" in base:
        raise ValueError("invalid filename")
    return base


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    caption: str | None = Form(default=None),
):
    """Accept one file + optional caption; return an honest receipt.

    PDF/DOCX/TXT uploads also get their text extracted, ingested as a
    searchable document, and fed to the fact-extraction pipeline —
    best-effort, never failing the upload itself.
    """
    from app import config
    # Operator scope for intake writes (see whatsapp._process_media):
    # Postgres checks INSERT ... RETURNING against the SELECT
    # policy, so restricted-scope facts need ceo+unlocked variables.
    from app.db import scoped_conn
    from app.store import ingest_document, ingest_media

    filename = _safe_filename(file.filename)
    media_dir = config.MEDIA_DIR
    os.makedirs(media_dir, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}_{filename}"
    storage_path = os.path.join(media_dir, stored_name)
    content = await file.read()
    with open(storage_path, "wb") as fh:
        fh.write(content)
    company_id = config.COMPANY_ID
    with scoped_conn("ceo", unlocked=True) as conn:
        media_id = ingest_media(
            conn,
            company_id,
            filename,
            file.content_type or "application/octet-stream",
            storage_path,
            caption=caption,
        )
        row = conn.execute(
            "SELECT embedded FROM media WHERE id = %s", (media_id,)
        ).fetchone()
    embedded = bool(row[0]) if row else False
    text_extracted = False
    facts_stored = 0
    try:
        from app.extract_text import extract_text

        extracted = extract_text(filename, content)
        if extracted:
            text_extracted = True
            with scoped_conn("ceo", unlocked=True) as conn:
                doc_id = ingest_document(
                    conn, company_id, f"upload:{media_id}", filename, extracted
                )
                try:
                    from app.llm import extract_and_store

                    facts_stored = len(
                        extract_and_store(
                            conn, company_id, extracted, "document",
                            document_id=doc_id,
                        )
                    )
                except Exception:
                    facts_stored = 0
    except Exception:
        pass
    logger.info(
        "upload stored media_id=%s embedded=%s text_extracted=%s facts=%s",
        media_id,
        embedded,
        text_extracted,
        facts_stored,
    )
    return {
        "media_id": media_id,
        "filename": filename,
        "embedded": embedded,
        "storage_path": storage_path,
        "text_extracted": text_extracted,
        "facts_stored": facts_stored,
    }
