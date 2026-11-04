"""Engine question response cache for WhatsApp.

Cache key = (question, role, sender, provider, unlocked)
- Answers are generated strictly by the engine and stored here.
- Never serves a CEO answer to an employee or vice versa.
- CEO code step is preserved: cached sensitive answer is only served when unlocked.
- Cleared whenever a new document is uploaded or a conflict changes.
"""

import json
import logging
import re
import threading
from pathlib import Path
from typing import Optional

_log = logging.getLogger("cache")
_lock = threading.Lock()

# In-memory fast cache: key -> answer
_CACHE = {}

_CACHE_FILE = Path(__file__).resolve().parent.parent / "scratch" / "engine_cache.json"


def _normalize_key(question: str, role: str, sender: str, provider: str, unlocked: bool = False) -> str:
    """Construct deterministic cache key from question + role + sender + provider + unlocked state."""
    q_norm = re.sub(r"[^\w\s]", "", (question or "").lower())
    q_norm = re.sub(r"\s+", " ", q_norm).strip()
    r_norm = (role or "employee").strip().lower()
    s_norm = (sender or "").strip()
    p_norm = (provider or "meta").strip().lower()
    u_norm = "1" if unlocked else "0"
    return f"{q_norm}::{r_norm}::{s_norm}::{p_norm}::{u_norm}"


def get_cached_answer(
    question: str,
    role: str,
    sender: str,
    provider: Optional[str] = None,
    unlocked: bool = False,
) -> Optional[str]:
    """Retrieve cached response if present. Thread-safe and instant (< 0.001s)."""
    if not question:
        return None
    if not provider:
        try:
            from app.llm import PROVIDER
            provider = PROVIDER
        except Exception:
            provider = "meta"

    key = _normalize_key(question, role, sender, provider, unlocked)
    with _lock:
        val = _CACHE.get(key)
        if val is not None:
            return val
    return None


def set_cached_answer(
    question: str,
    role: str,
    sender: str,
    provider: str,
    answer: str,
    unlocked: bool = False,
) -> None:
    """Store an engine-generated answer in the cache."""
    if not question or not answer:
        return
    key = _normalize_key(question, role, sender, provider, unlocked)
    with _lock:
        _CACHE[key] = answer
        _persist_cache()


def clear_cache(company_id: Optional[str] = None) -> None:
    """Clear all cache entries when documents or conflicts change."""
    with _lock:
        _CACHE.clear()
        try:
            if _CACHE_FILE.is_file():
                _CACHE_FILE.unlink()
        except Exception:
            pass
    _log.info("Engine question cache cleared (company_id=%s)", company_id)


def _persist_cache() -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_CACHE, f)
    except Exception:
        pass


def _load_cache() -> None:
    global _CACHE
    if _CACHE_FILE.is_file():
        try:
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    with _lock:
                        _CACHE.update(data)
        except Exception:
            pass


_load_cache()
