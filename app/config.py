"""Environment-only configuration. No secrets live in code.

The founder pastes keys himself (Meta Model API, WhatsApp token, DB URL).
This module only reads names; it never prints values.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Resolve .env from repo root, the folder containing the app/ package
_REPO_ROOT_ENV = Path(__file__).resolve().parent.parent / ".env"
_LOADED_ENV_PATH = None

try:
    from dotenv import load_dotenv

    # Explicit path to repo-root .env with override=True
    if _REPO_ROOT_ENV.is_file():
        load_dotenv(dotenv_path=_REPO_ROOT_ENV, override=True)
        _LOADED_ENV_PATH = str(_REPO_ROOT_ENV.resolve())
    else:
        # Fallback to working directory lookup only if repo-root file does not exist
        _cwd_env = Path.cwd() / ".env"
        if _cwd_env.is_file():
            load_dotenv(dotenv_path=_cwd_env, override=True)
            _LOADED_ENV_PATH = str(_cwd_env.resolve())
        else:
            load_dotenv(override=True)

    if _LOADED_ENV_PATH:
        logger.info("Loaded .env from: %s", _LOADED_ENV_PATH)
    else:
        logger.info("No .env file found to load")
except ImportError:
    logger.warning("python-dotenv is not installed; .env loading skipped")

LOADED_ENV_PATH = _LOADED_ENV_PATH

_DEFAULTS = {
    "DATABASE_URL": "postgresql://brain:brain@localhost:5432/brain",
    "OLLAMA_URL": "http://localhost:11434",
    "EMBED_MODEL": "embeddinggemma",
    "EMBED_DIMS": 768,
    "META_API_BASE": "https://api.meta.ai/v1",
    "META_API_KEY": "",
    "META_MODEL": "muse-spark-1.3",
    "WHATSAPP_TOKEN": "",
    "WHATSAPP_ACCESS_TOKEN": "",
    "WHATSAPP_PHONE_ID": "",
    "WHATSAPP_PHONE_NUMBER_ID": "",
    "WHATSAPP_TEST_RECIPIENT": "",
    "WHATSAPP_VERIFY_TOKEN": "",
    "WHATSAPP_APP_SECRET": "",
    "WHATSAPP_CEO_PHONE": "",
    "WHATSAPP_CFO_PHONE": "",
    "WHATSAPP_CTO_PHONE": "",
    "WHATSAPP_MANAGER_PHONE": "",
    "COMPANY_ID": "sample-company",
    "MEDIA_DIR": "./media",
    "AGENT_NAME": "Pharos",
}


def agent_name() -> str:
    """The agent's name lives in exactly ONE configurable place."""
    return get("AGENT_NAME", "Pharos")


def intro_message() -> str:
    """Introductory capabilities reply using the single configured name."""
    name = agent_name()
    return (
        f"Hi, I'm {name}, your corporate agent.\n"
        "I can answer questions from your company documents, including reimbursement and anything else in them.\n"
        "I can flag when documents disagree, and I can ping HR or the right employee about an issue."
    )


def get(name: str, default: str = "") -> str:
    """Read an environment variable, falling back to default or known defaults."""
    if name in ("WHATSAPP_TOKEN", "WHATSAPP_ACCESS_TOKEN"):
        return os.environ.get("WHATSAPP_ACCESS_TOKEN") or os.environ.get("WHATSAPP_TOKEN") or default or ""
    if name in ("WHATSAPP_PHONE_ID", "WHATSAPP_PHONE_NUMBER_ID"):
        return os.environ.get("WHATSAPP_PHONE_NUMBER_ID") or os.environ.get("WHATSAPP_PHONE_ID") or default or ""
    return os.environ.get(name, default or _DEFAULTS.get(name, ""))


def __getattr__(name: str):
    """Dynamic module attribute access ensuring live os.environ values."""
    if name in ("WHATSAPP_TOKEN", "WHATSAPP_ACCESS_TOKEN"):
        return os.environ.get("WHATSAPP_ACCESS_TOKEN") or os.environ.get("WHATSAPP_TOKEN") or ""
    if name in ("WHATSAPP_PHONE_ID", "WHATSAPP_PHONE_NUMBER_ID"):
        return os.environ.get("WHATSAPP_PHONE_NUMBER_ID") or os.environ.get("WHATSAPP_PHONE_ID") or ""
    if name in _DEFAULTS:
        val = os.environ.get(name)
        if val is not None:
            if name == "EMBED_DIMS":
                try:
                    return int(val)
                except ValueError:
                    return 768
            return val
        return _DEFAULTS[name]
    val = os.environ.get(name)
    if val is not None:
        return val
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def __dir__():
    return list(globals().keys()) + list(_DEFAULTS.keys())
