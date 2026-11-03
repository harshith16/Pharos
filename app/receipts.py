"""Receipt photo to action (P1-4).

Trigger: an image message whose caption contains "receipt".
Anything else keeps the normal media path, so a policy photo
that happens to contain amounts never emails HR by accident.

Vision order: the Meta model transcription first (existing
app.llm.transcribe_image); when it yields nothing, a local vision
model via Ollama (extraction ONLY) at OLLAMA_URL (default
http://localhost:11434), model from OLLAMA_VISION_MODEL (default
"moondream"). When both yield nothing usable, the sender is told
plainly — fields are never faked, partial, or guessed.

Fields are parsed deterministically from the transcription
(regexes, no model): vendor, amount, date. All three must parse
or the receipt is unreadable. The email goes through the real
Gmail connect flow (zero .env auth) to the founder-configured
recipient (connect-page stored default first, then the
address-only fallback), and every attempt writes an email_actions
audit row via the shared actions audit helper.
"""

import base64
import json
import logging
import os
import re
import urllib.request
from datetime import date

_log = logging.getLogger(__name__)

COULD_NOT_READ_REPLY = (
    "I couldn't read the receipt. Please send a clearer photo."
)

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3,
    "march": 3, "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11,
    "november": 11, "dec": 12, "december": 12,
}

_AMOUNT_RE = re.compile(r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d{1,2})?)")
_DATE_NUM_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")
_DATE_DMY_RE = re.compile(
    r"\b(\d{1,2})\s+"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+(\d{4})\b",
    re.IGNORECASE,
)
_DATE_MDY_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+(\d{1,2}),\s+(\d{4})\b",
    re.IGNORECASE,
)
_SKIP_VENDOR = {
    "tax invoice", "invoice", "receipt", "cash memo", "bill",
    "gst invoice", "tax receipt", "retail invoice", "sale receipt",
}
_NON_VENDOR_PREFIX = (
    "total", "subtotal", "sub total", "grand total", "gst", "cgst",
    "sgst", "igst", "tax", "amount", "balance", "phone", "tel",
    "mobile", "email", "address", "gstin", "date", "bill no",
    "receipt no", "invoice no", "item", "qty", "price",
)


def is_receipt_caption(caption) -> bool:
    """True when the image caption marks the photo as a receipt."""
    return bool(caption) and "receipt" in str(caption).lower()


def _parse_amount(text: str):
    """Currency-marked amount: the TOTAL line's amount when one
    exists, else the largest currency amount. Returns (display, value)
    or None. A bare number without a currency marker is never an
    amount — quantities and dates must not become money."""
    total_value = None
    total_display = None
    best_value = None
    best_display = None
    for line in text.splitlines():
        for match in _AMOUNT_RE.finditer(line):
            raw = match.group(1).replace(",", "")
            try:
                value = float(raw)
            except ValueError:
                continue
            if "." in raw:
                display = f"₹{value:,.2f}"
            else:
                display = f"₹{value:,.0f}"
            if best_value is None or value > best_value:
                best_value, best_display = value, display
            if "total" in line.lower():
                total_value, total_display = value, display
    if total_value is not None:
        return total_display, total_value
    if best_value is not None:
        return best_display, best_value
    return None


def _valid_ymd(year: int, month: int, day: int):
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _parse_date(text: str):
    """First valid date, normalised to YYYY-MM-DD. Numeric dates
    read day-first (Indian receipts); if that is invalid, month-first
    is tried. Month names are unambiguous."""
    for match in _DATE_NUM_RE.finditer(text):
        first, second, year = (int(match.group(1)), int(match.group(2)),
                               int(match.group(3)))
        parsed = _valid_ymd(year, second, first)
        if parsed is None:
            parsed = _valid_ymd(year, first, second)
        if parsed is not None:
            return parsed
    for match in _DATE_DMY_RE.finditer(text):
        month = _MONTHS[match.group(2).lower()]
        parsed = _valid_ymd(int(match.group(3)), month, int(match.group(1)))
        if parsed is not None:
            return parsed
    for match in _DATE_MDY_RE.finditer(text):
        month = _MONTHS[match.group(1).lower()]
        parsed = _valid_ymd(int(match.group(3)), month, int(match.group(2)))
        if parsed is not None:
            return parsed
    return None


def _parse_vendor(text: str):
    """First content line that is not a header, total, tax, date,
    or amount line. Returns None when no such line exists."""
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("*#-|").strip()
        if len(line) < 2 or len(line) > 80:
            continue
        lowered = re.sub(r"[^a-z ]", "", line.lower()).strip()
        if lowered in _SKIP_VENDOR:
            continue
        if any(lowered.startswith(prefix) for prefix in _NON_VENDOR_PREFIX):
            continue
        if _AMOUNT_RE.search(line) or _DATE_NUM_RE.search(line):
            continue
        if not re.search(r"[A-Za-z]", line):
            continue
        return line
    return None


def extract_receipt_fields(text: str):
    """(vendor, amount, date) from a transcription, or None unless
    all three parse. Never partial, never invented."""
    if not text or not text.strip():
        return None
    amount = _parse_amount(text)
    parsed_date = _parse_date(text)
    vendor = _parse_vendor(text)
    if amount is None or parsed_date is None or vendor is None:
        return None
    return {
        "vendor": vendor,
        "amount": amount[0],
        "date": parsed_date,
    }


def transcribe_via_ollama(image_bytes: bytes, mime_type: str):
    """Local vision model via Ollama's /api/chat, extraction ONLY.
    Returns transcription text or None. Never raises: an unreachable
    server, a missing model, or an empty reply all mean None."""
    if not image_bytes:
        return None
    try:
        from app import config as _config

        base = (
            os.environ.get("OLLAMA_URL")
            or getattr(_config, "OLLAMA_URL", "")
            or "http://localhost:11434"
        )
    except Exception:
        base = "http://localhost:11434"
    model = os.environ.get("OLLAMA_VISION_MODEL") or "moondream"
    payload = {
        "model": model,
        "stream": False,
        "messages": [{
            "role": "user",
            "content": (
                "Transcribe all visible text from this receipt image "
                "accurately: vendor name, amounts, and date. Output "
                "only the transcription."
            ),
            "images": [base64.b64encode(image_bytes).decode("utf-8")],
        }],
    }
    try:
        request = urllib.request.Request(
            base.rstrip("/") + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = ((data.get("message") or {}).get("content") or "").strip()
        return content or None
    except Exception as exc:
        _log.info("Ollama vision fallback unavailable: %s", exc)
        return None


def process_receipt(conn, company_id: str, sender: str, sender_role: str,
                    transcription, image_bytes, mime_type: str) -> str:
    """Run one receipt photo to its HR email; return the WhatsApp
    reply. Never raises and never reports a send that did not happen."""
    try:
        text = (transcription or "").strip()
        if not text and image_bytes:
            text = (transcribe_via_ollama(image_bytes, mime_type) or "").strip()
        fields = extract_receipt_fields(text) if text else None
        if fields is None:
            _log.info(
                "receipt unreadable company=%s sender=%s", company_id, sender
            )
            return COULD_NOT_READ_REPLY
        vendor = fields["vendor"]
        amount = fields["amount"]
        parsed_date = fields["date"]
        topic = f"receipt {vendor} {amount} {parsed_date}"

        from app import emailer, gmail_api
        from app.actions import _audit, EMAIL_NOT_CONFIGURED_REPLY

        try:
            gmail_ready = gmail_api.is_gmail_ready()
        except Exception:
            gmail_ready = False
        if not gmail_ready:
            _audit(conn, company_id, sender, sender_role, "HR", topic,
                   "", "not_configured")
            return EMAIL_NOT_CONFIGURED_REPLY
        recipient, _key = emailer.resolve_recipient("HR")
        if not recipient:
            _audit(conn, company_id, sender, sender_role, "HR", topic,
                   "", "no_recipient")
            return (
                "I don't have an email address configured for HR, "
                "so I couldn't send that receipt."
            )
        subject = f"Expense receipt from {vendor}"
        body = (
            "Receipt details:\n"
            f"Vendor: {vendor}\n"
            f"Amount: {amount}\n"
            f"Date: {parsed_date}\n\n"
            f"Sent by {sender} via WhatsApp."
        )
        ok, error = gmail_api.send_via_gmail(recipient, subject, body)
        if ok:
            _audit(conn, company_id, sender, sender_role, "HR", topic,
                   recipient, "sent_gmail")
            return (
                f"Done — receipt sent to HR: {vendor}, {amount}, "
                f"{parsed_date}."
            )
        _log.error("receipt email failed error=%s", error)
        _audit(conn, company_id, sender, sender_role, "HR", topic,
               recipient, "failed", error or "")
        return (
            f"I read the receipt ({vendor}, {amount}, {parsed_date}) "
            "but couldn't send the email to HR. Please try again later."
        )
    except Exception:
        _log.exception("process_receipt failed")
        return "I couldn't send that receipt. Please try again later."
