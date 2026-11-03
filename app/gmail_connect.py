"""Gmail connect flow: browser login, zero auth in .env.

The founder opens /auth/gmail in a browser, logs in with Google,
and approves the gmail.send scope only. The callback exchanges
the code for tokens and saves them to a gitignored local file
(data/gmail_token.json). Nothing auth-related ever goes in .env.

Client credentials come from ONE standard file: credentials.json
in the repo folder — the JSON downloaded from Google Cloud
Console when creating an OAuth client ID. It is gitignored. If
it is missing, the connect page says so plainly and tells the
founder what to download and where to put it. No IDs are ever
printed, logged, or pasted into .env.

Endpoints:
- GET  /auth/gmail          — connect page (form + status)
- GET  /auth/gmail/start    — begins Google OAuth (redirect)
- GET  /auth/gmail/callback — Google returns here; saves tokens
- GET  /auth/gmail/status   — JSON: connected or not (no secrets)

The redirect URI is computed from the incoming request, and the
connect page displays it exactly, so the founder registers that
same URI as an authorized redirect URI in Google Cloud. Pending
state (including an optional HR recipient) lives only in memory
and expires after 10 minutes; a server restart just means
reconnecting, never a leaked secret.
"""

import html
import logging
import secrets
import time
import urllib.parse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import gmail_api

_log = logging.getLogger("gmail_connect")
router = APIRouter()

_STATE_TTL_SECONDS = 600
_pending: dict = {}


def _redirect_uri(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    return base + "/auth/gmail/callback"


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title></head><body>"
        f"<h1>{html.escape(title)}</h1>{body}</body></html>"
    )


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Google OAuth authorization URL for the gmail.send scope."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": gmail_api.GMAIL_SEND_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return gmail_api.OAUTH_AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str, client_id: str, client_secret: str,
                  redirect_uri: str, token_url: str = gmail_api.OAUTH_TOKEN_URL):
    """Exchange an authorization code for tokens.

    Returns (token_dict, None) or (None, error). Never raises, and
    never logs token contents.
    """
    try:
        resp = httpx.post(
            token_url,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=30.0,
            trust_env=False,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return None, str(exc)
    if not isinstance(data, dict) or not (
        data.get("access_token") or data.get("refresh_token")
    ):
        return None, "token response carried no tokens"
    return data, None


@router.get("/auth/gmail", response_class=HTMLResponse)
async def connect_page(request: Request):
    """Connect page: status, redirect URI, optional HR recipient."""
    creds = gmail_api.load_credentials()
    redirect_uri = _redirect_uri(request)
    connected = gmail_api.is_gmail_ready()
    status = (
        "<p><b>Status: Gmail is connected.</b> "
        "This bot can send email. Reconnecting replaces the saved token.</p>"
        if connected
        else "<p><b>Status: Gmail is not connected.</b> "
        "Connect below so the bot can send email.</p>"
    )
    if creds is None:
        return _page(
            "Connect Gmail",
            status
            + "<p><b>Missing file: credentials.json</b></p>"
            "<p>To connect Gmail, download the OAuth client file and "
            "save it as <b>credentials.json</b> in the repo folder "
            "(the same folder that holds the app folder):</p>"
            "<ol><li>Open Google Cloud Console, then APIs &amp; Services "
            "-&gt; Credentials.</li>"
            "<li>Create an OAuth client ID (Desktop app, or Web "
            "application).</li>"
            "<li>Download its JSON file and save it as "
            "<b>credentials.json</b> in the repo folder.</li>"
            "<li>For a Web application client, also add this exact "
            "redirect URI as an authorized redirect URI:</li></ol>"
            f"<p><code>{html.escape(redirect_uri)}</code></p>"
            "<p>Then reload this page. No IDs go in .env, ever.</p>",
        )
    return _page(
        "Connect Gmail",
        status
        + "<p>Before continuing, add this exact redirect URI as an "
        "authorized redirect URI in Google Cloud (for a Web "
        "application client; a Desktop app client does not need it):</p>"
        f"<p><code>{html.escape(redirect_uri)}</code></p>"
        "<form action='/auth/gmail/start' method='get'>"
        "<label>HR email (optional, recipient for HR requests): "
        "<input type='email' name='hr_email'></label><br><br>"
        "<button type='submit'>Connect Gmail</button></form>"
        "<p>Google will ask you to log in and approve sending email "
        "only (gmail.send scope). Nothing else is requested.</p>",
    )


@router.get("/auth/gmail/start")
async def start_oauth(request: Request, hr_email: str = ""):
    """Begin Google OAuth; pending state lives in memory only."""
    creds = gmail_api.load_credentials()
    if creds is None:
        return _page(
            "Connect Gmail",
            "<p>Missing file: credentials.json in the repo folder. "
            "See the connect page for what to download.</p>",
        )
    state = secrets.token_urlsafe(24)
    _pending[state] = {
        "created": time.time(),
        "hr_email": (hr_email or "").strip(),
    }
    url = build_auth_url(creds["client_id"], _redirect_uri(request), state)
    return RedirectResponse(url=url, status_code=303)


@router.get("/auth/gmail/callback", response_class=HTMLResponse)
async def oauth_callback(request: Request, code: str = "", state: str = "",
                         error: str = ""):
    """Google returns here: validate state, save tokens, confirm."""
    if error:
        return _page(
            "Gmail connection failed",
            "<p>Google did not approve the connection. "
            "Reload the connect page and try again.</p>",
        )
    pending = _pending.pop(state, None) if state else None
    if pending is None or (time.time() - pending.get("created", 0) > _STATE_TTL_SECONDS):
        return _page(
            "Gmail connection failed",
            "<p>This connection link expired or is invalid. "
            "Start again from the connect page.</p>",
        )
    creds = gmail_api.load_credentials()
    if creds is None:
        return _page(
            "Gmail connection failed",
            "<p>Missing file: credentials.json in the repo folder.</p>",
        )
    if not code:
        return _page(
            "Gmail connection failed",
            "<p>No authorization code was returned. Try again.</p>",
        )
    token_data, err = exchange_code(
        code, creds["client_id"], creds["client_secret"],
        _redirect_uri(request),
    )
    if token_data is None:
        _log.warning("gmail code exchange failed: %s", err)
        return _page(
            "Gmail connection failed",
            "<p>The code exchange failed. Reload the connect page "
            "and try again.</p>",
        )
    token_data["client_id"] = creds["client_id"]
    token_data["client_secret"] = creds["client_secret"]
    if not gmail_api.save_token(token_data):
        return _page(
            "Gmail connection failed",
            "<p>Connected at Google, but the token could not be "
            "saved on this server. Check the server log and retry.</p>",
        )
    hr_email = pending.get("hr_email") or ""
    if hr_email:
        # Stored as the default recipient: the founder entered one
        # mailbox on the connect page, and it applies to any target
        # without a more specific saved recipient. No role name is
        # hard-coded here; routing still resolves targets from data.
        gmail_api.save_recipients({"DEFAULT": hr_email})
    return _page(
        "Gmail connected",
        "<p><b>Gmail is connected.</b> This bot can now send email. "
        "You can close this page and return to WhatsApp.</p>",
    )


@router.get("/auth/gmail/status")
async def connect_status():
    """JSON status only — never any token or credential value."""
    recipients = gmail_api.load_recipients()
    return JSONResponse({
        "connected": gmail_api.is_gmail_ready(),
        "has_recipient": bool(recipients),
    })
