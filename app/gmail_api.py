"""Gmail API send via the connect flow: the ONLY email path.

The founder never puts auth in .env. He opens /auth/gmail in a
browser, logs in with Google, and approves the gmail.send scope.
The callback saves the tokens to a gitignored local file
(data/gmail_token.json); sending reads that file. Client
credentials come from a single standard Google Cloud download
(credentials.json in the repo folder, gitignored) — never from
.env, never printed anywhere.

Sending uses Gmail API users.messages.send with an OAuth access
token, refreshing it from the stored refresh token when it
expires. No new packages: plain httpx calls, and the raw message
is a standard RFC 822 message encoded base64url.

Behavior, all honest:
- "Connected" means: the token file exists AND carries a refresh
  token or an access token. Anything less is "not connected" and
  the caller replies so plainly — never a fake "sent".
- A 401 from Gmail triggers exactly one refresh-and-retry. A
  second failure is returned as (False, error) — never a fake
  success.
- Token refresh writes the new access token back to the token
  file (refresh token preserved). A write failure is logged, not
  fatal to the send itself.
- No function in this module reads Gmail auth from .env, and no
  SMTP path exists anywhere in the app anymore.
"""

import base64
import json
import logging
import os
from pathlib import Path

import httpx

_log = logging.getLogger("gmail_api")

GMAIL_API_BASE = "https://gmail.googleapis.com"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def repo_root() -> Path:
    """Repo folder: the parent of the app/ package."""
    return Path(__file__).resolve().parent.parent


def token_path_default() -> Path:
    return repo_root() / "data" / "gmail_token.json"


def credentials_path_default() -> Path:
    return repo_root() / "credentials.json"


def connect_path_default() -> Path:
    return repo_root() / "data" / "gmail_connect.json"


def _read_json_file(path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_json_file(path, data: dict) -> bool:
    """Write JSON, creating parent dirs; mode 0600 best-effort."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        try:
            os.chmod(target, 0o600)
        except Exception:
            pass
        return True
    except Exception as exc:
        _log.warning("could not write %s: %s", target, exc)
        return False


def load_credentials(credentials_path=None) -> dict | None:
    """Google Cloud download: {"installed"|"web": {client_id, ...}}.

    Returns {"client_id", "client_secret", "token_uri"} or None.
    Values are never logged.
    """
    path = Path(credentials_path) if credentials_path else credentials_path_default()
    data = _read_json_file(path)
    if data is None:
        return None
    inner = data.get("installed") or data.get("web") or {}
    client_id = (inner.get("client_id") or "").strip() if isinstance(inner, dict) else ""
    client_secret = (inner.get("client_secret") or "").strip() if isinstance(inner, dict) else ""
    token_uri = (inner.get("token_uri") or "").strip() if isinstance(inner, dict) else ""
    if not client_id or not client_secret:
        return None
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "token_uri": token_uri or OAUTH_TOKEN_URL,
    }


def load_token(token_path=None) -> dict | None:
    """Saved token file as {"access_token", "refresh_token", "raw"}.

    Accepts the standard Google format ("token" for the access
    token) and the plain "access_token" spelling. Contents are
    never logged.
    """
    path = Path(token_path) if token_path else token_path_default()
    data = _read_json_file(path)
    if data is None:
        return None
    access = data.get("token") or data.get("access_token")
    if not isinstance(access, str) or not access.strip():
        access = None
    refresh = data.get("refresh_token")
    if not isinstance(refresh, str) or not refresh.strip():
        refresh = None
    if access is None and refresh is None:
        return None
    return {"access_token": access, "refresh_token": refresh, "raw": data}


def save_token(token_data: dict, token_path=None) -> bool:
    """Persist a token dict from the connect callback."""
    path = Path(token_path) if token_path else token_path_default()
    return _write_json_file(path, dict(token_data or {}))


def is_gmail_ready(token_path=None) -> bool:
    """True only when a saved token could actually be attempted."""
    return load_token(token_path) is not None


def load_recipients(connect_path=None) -> dict:
    """Recipients saved on the connect page: {UPPER NAME: address}.

    Stored in data/gmail_connect.json, never in .env. Returns {}
    when nothing was saved.
    """
    path = Path(connect_path) if connect_path else connect_path_default()
    data = _read_json_file(path)
    if data is None:
        return {}
    recipients = data.get("recipients")
    if not isinstance(recipients, dict):
        return {}
    out = {}
    for key, value in recipients.items():
        if isinstance(key, str) and isinstance(value, str) and value.strip():
            out[key.strip().upper()] = value.strip()
    return out


def save_recipients(recipients: dict, connect_path=None) -> bool:
    """Merge recipients into the connect file, preserving the rest."""
    path = Path(connect_path) if connect_path else connect_path_default()
    existing = _read_json_file(path) or {}
    merged = dict(existing.get("recipients") or {})
    for key, value in (recipients or {}).items():
        if isinstance(key, str) and isinstance(value, str) and value.strip():
            merged[key.strip().upper()] = value.strip()
    existing["recipients"] = merged
    return _write_json_file(path, existing)


def build_raw_message(to: str, subject: str, body: str) -> str:
    """RFC 822 message -> base64url string for users.messages.send."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["To"] = to.strip()
    message["Subject"] = subject
    message.set_content(body or "")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def _refresh_access_token(token: dict, token_url: str, token_path,
                          credentials_path=None) -> str | None:
    """Exchange the refresh token for a new access token, or None."""
    refresh = token.get("refresh_token")
    if not refresh:
        return None
    raw = token.get("raw") or {}
    client_id = (raw.get("client_id") or "").strip()
    client_secret = (raw.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        creds = load_credentials(credentials_path)
        if creds is None:
            return None
        client_id = creds["client_id"]
        client_secret = creds["client_secret"]
    try:
        resp = httpx.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=30.0,
            trust_env=False,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        _log.warning("gmail token refresh failed: %s", exc)
        return None
    new_access = data.get("access_token")
    if not isinstance(new_access, str) or not new_access.strip():
        return None
    updated = dict(raw)
    updated["token"] = new_access.strip()
    updated["access_token"] = new_access.strip()
    _write_json_file(token_path, updated)
    return new_access.strip()


def _send_once(access_token: str, raw: str, api_base: str):
    """One users.messages.send call. Returns (status, error)."""
    url = api_base.rstrip("/") + "/gmail/v1/users/me/messages/send"
    try:
        resp = httpx.post(
            url,
            json={"raw": raw},
            headers={
                "Authorization": "Bearer " + access_token,
                "Content-Type": "application/json",
            },
            timeout=30.0,
            trust_env=False,
        )
    except Exception as exc:
        return None, str(exc)
    if resp.status_code in (200, 201):
        return resp.status_code, None
    return resp.status_code, f"gmail api returned {resp.status_code}"


def send_via_gmail(to: str, subject: str, body: str,
                   api_base: str = GMAIL_API_BASE,
                   token_url: str = OAUTH_TOKEN_URL,
                   token_path=None, credentials_path=None):
    """Send one real email through the Gmail API, using the saved token.

    Returns (True, None) or (False, error). Exactly one
    refresh-and-retry happens on a 401; anything else failing is
    returned honestly. api_base/token_url/token_path exist so
    tests can point the real HTTP calls at local servers and temp
    files; production uses the Google defaults and the connect
    flow's saved token file. Never raises, never logs token values.
    """
    path = Path(token_path) if token_path else token_path_default()
    token = load_token(path)
    if token is None:
        return False, "gmail is not connected"
    if not to or not to.strip():
        return False, "no recipient address"
    raw = build_raw_message(to, subject, body)
    access = token.get("access_token")
    if access:
        status, error = _send_once(access, raw, api_base)
        if status in (200, 201):
            return True, None
        if status != 401:
            return False, error or "gmail send failed"
        # 401: exactly one refresh-and-retry.
    new_access = _refresh_access_token(token, token_url, path, credentials_path)
    if not new_access:
        return False, "gmail access token expired and refresh failed"
    status, error = _send_once(new_access, raw, api_base)
    if status in (200, 201):
        return True, None
    return False, error or "gmail send failed"
