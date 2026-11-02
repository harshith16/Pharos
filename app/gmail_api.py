"""Gmail API send via OAuth: the PRIMARY real email path.

Sends through the Gmail API (users.messages.send) with an OAuth
access token, refreshing it from the stored refresh token when it
expires. No new packages: plain httpx calls against the Gmail REST
API, and the raw message is a standard RFC 822 message encoded
base64url, exactly as the API requires.

Configuration comes from .env ONLY (names, never values, never
logged):
- GMAIL_OAUTH_CLIENT_ID, GMAIL_OAUTH_CLIENT_SECRET
- GMAIL_OAUTH_TOKEN_FILE: path to the OAuth token JSON (the standard
  Google format: access token under "token" or "access_token",
  "refresh_token", "token_uri", "client_id", "client_secret",
  "scopes", "expiry"). The founder runs the OAuth consent flow on
  his own machine and places this file; this module never performs
  an interactive consent flow and never prints token contents.

Behavior, all honest:
- OAuth "configured" means: client id + secret present AND the token
  file exists AND it carries a refresh token or an access token.
  Anything less is "not configured" and the caller falls back to
  SMTP (the secondary path), or to the not-configured reply when
  SMTP is also absent.
- A 401 from Gmail triggers exactly one refresh-and-retry. A second
  failure is returned as (False, error) — never a fake success, and
  never a silent SMTP retry after an OAuth attempt (that could
  double-send if Gmail actually accepted the first attempt but the
  response was lost).
- Token refresh writes the new access token back to the token file
  (refresh token preserved). A write failure is logged, not fatal
  to the send itself.
"""

import base64
import json
import logging
import os

import httpx

from app import config

_log = logging.getLogger("gmail_api")

GMAIL_API_BASE = "https://gmail.googleapis.com"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def get_oauth_config() -> dict | None:
    """OAuth config from .env, or None when keys are missing.

    Returns None when client id, client secret, or token file path
    is absent. The token file itself is read separately, so a
    missing file is also "not configured" (see is_oauth_ready).
    """
    client_id = (config.get("GMAIL_OAUTH_CLIENT_ID") or "").strip()
    client_secret = (config.get("GMAIL_OAUTH_CLIENT_SECRET") or "").strip()
    token_file = (config.get("GMAIL_OAUTH_TOKEN_FILE") or "").strip()
    if not client_id or not client_secret or not token_file:
        return None
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "token_file": token_file,
    }


def _read_token_file(path: str) -> dict | None:
    """Token JSON as a dict, or None when missing/unreadable.

    Accepts the standard Google format ("token" for the access
    token) and the plain "access_token" spelling. Contents are
    never logged.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
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


def _write_token_file(path: str, raw: dict, access_token: str) -> None:
    """Persist a refreshed access token, preserving everything else."""
    try:
        updated = dict(raw or {})
        updated["token"] = access_token
        updated["access_token"] = access_token
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(updated, fh)
    except Exception as exc:
        _log.warning("could not write refreshed token file: %s", exc)


def is_oauth_ready(oauth_config: dict | None = None) -> bool:
    """True only when OAuth could actually be attempted."""
    if oauth_config is None:
        oauth_config = get_oauth_config()
    if oauth_config is None:
        return False
    token = _read_token_file(oauth_config["token_file"])
    return token is not None


def build_raw_message(to: str, subject: str, body: str) -> str:
    """RFC 822 message -> base64url string for users.messages.send."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["To"] = to.strip()
    message["Subject"] = subject
    message.set_content(body or "")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def _refresh_access_token(oauth_config: dict, token: dict,
                          token_url: str) -> str | None:
    """Exchange the refresh token for a new access token, or None."""
    refresh = token.get("refresh_token")
    if not refresh:
        return None
    try:
        resp = httpx.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": oauth_config["client_id"],
                "client_secret": oauth_config["client_secret"],
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
    _write_token_file(
        oauth_config["token_file"], token.get("raw") or {}, new_access.strip()
    )
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
                   oauth_config: dict | None = None,
                   api_base: str = GMAIL_API_BASE,
                   token_url: str = OAUTH_TOKEN_URL):
    """Send one real email through the Gmail API.

    Returns (True, None) or (False, error). Exactly one
    refresh-and-retry happens on a 401; anything else failing is
    returned honestly. api_base/token_url exist so tests can point
    the real HTTP calls at local servers; production uses the
    Google defaults. Never raises, never logs token values.
    """
    if oauth_config is None:
        oauth_config = get_oauth_config()
    if oauth_config is None:
        return False, "gmail oauth is not configured"
    token = _read_token_file(oauth_config["token_file"])
    if token is None:
        return False, "gmail oauth token file is missing or invalid"
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
    new_access = _refresh_access_token(oauth_config, token, token_url)
    if not new_access:
        return False, "gmail access token expired and refresh failed"
    status, error = _send_once(new_access, raw, api_base)
    if status in (200, 201):
        return True, None
    return False, error or "gmail send failed"
