"""Company brain API entrypoint.

Run with: uvicorn app.main:app --port 8000

Structured logging is configured once here; module loggers record ids
and outcomes only — never tokens, secrets, or message bodies.
"""

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

from fastapi import FastAPI

from app import config
from app.embed import vectors_available
from app.gmail_connect import router as gmail_connect_router
from app.graph import router as graph_router
from app.timeline import router as timeline_router
from app.upload import router as upload_router
from app.whatsapp import router as whatsapp_router

app = FastAPI(title="company-brain")
app.include_router(whatsapp_router)
app.include_router(gmail_connect_router)
app.include_router(timeline_router)
app.include_router(upload_router)
app.include_router(graph_router)


@app.get("/health")
async def health():
    return {"status": "ok", "vectors": vectors_available()}


@app.get("/")
async def root():
    return {"service": "company-brain", "status": "ok"}
