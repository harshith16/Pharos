"""Targeted proofs for receipt photo to action (P1-4).

Prerequisites: base schemas + schema_actions.sql applied,
DATABASE_URL set. No model key, no real Gmail: the Meta vision
text and the Gmail send are patched; the Ollama fallback is
proven against a local stub server speaking /api/chat.

Sample receipt (fictional):
  TAX INVOICE / Sharma General Store / 12 MG Road, Bengaluru /
  Item A Rs. 350 / Item B Rs. 900 / TOTAL Rs. 1,250 /
  Date: 02/11/2026 / GSTIN ...

Checks:
(a) field extraction: exact vendor/amount/date; missing any one
    field -> None (never partial).
(b) process_receipt success: exact WhatsApp confirmation, email
    body carries all three fields, audit row sent_gmail.
(c) unreadable (no transcription, Ollama unreachable): exact
    could-not-read reply, no send attempted.
(d) Ollama fallback plumbing: stub /api/chat transcription is
    used and the send happens.
(e) simulated WhatsApp round-trip: captioned receipt image ->
    success confirmation.
(f) negative control: uncaptioned image -> normal media path,
    no HR email, no receipt confirmation.

Run: PYTHONPATH=. python scripts/test_receipts.py
Exit code 0 iff every check passes.
"""

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import receipts as receipts_mod
from app.db import get_conn

COMPANY = "receipt-co"
RESULTS = []

SAMPLE = (
    "TAX INVOICE\n"
    "Sharma General Store\n"
    "12 MG Road, Bengaluru\n"
    "Item A - Rs. 350\n"
    "Item B - Rs. 900\n"
    "TOTAL Rs. 1,250\n"
    "Date: 02/11/2026\n"
    "GSTIN 29ABCDE1234F1Z5\n"
)


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))


def main():
    # (a) Extraction: exact fields; any missing field -> None.
    fields = receipts_mod.extract_receipt_fields(SAMPLE)
    check(
        "a-extract-exact",
        fields == {
            "vendor": "Sharma General Store",
            "amount": "₹1,250",
            "date": "2026-11-02",
        },
        f"fields={fields}",
    )
    check(
        "a-missing-amount-none",
        receipts_mod.extract_receipt_fields(
            "Sharma General Store\nDate: 02/11/2026\n"
        ) is None,
        "no currency -> None",
    )
    check(
        "a-missing-date-none",
        receipts_mod.extract_receipt_fields(
            "Sharma General Store\nTOTAL Rs. 1,250\n"
        ) is None,
        "no date -> None",
    )
    check(
        "a-missing-vendor-none",
        receipts_mod.extract_receipt_fields(
            "TAX INVOICE\nTOTAL Rs. 500\nDate: 02/11/2026\n"
        ) is None,
        "header+total only -> None",
    )

    import app.gmail_api as gmail_api_mod

    real_ready = gmail_api_mod.is_gmail_ready
    real_send = gmail_api_mod.send_via_gmail
    real_load = gmail_api_mod.load_recipients
    sent = []

    gmail_api_mod.is_gmail_ready = lambda: True
    gmail_api_mod.load_recipients = lambda: {"DEFAULT": "hr@example.com"}

    def _capture_send(recipient, subject, body):
        sent.append((recipient, subject, body))
        return True, None

    gmail_api_mod.send_via_gmail = _capture_send
    try:
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM email_actions WHERE company_id = %s", (COMPANY,)
            )
            conn.commit()
            # (b) Success path.
            reply_b = receipts_mod.process_receipt(
                conn, COMPANY, "910000000401", "employee", SAMPLE,
                b"fake-bytes", "image/jpeg",
            )
            audit = conn.execute(
                "SELECT status, recipient FROM email_actions "
                "WHERE company_id = %s ORDER BY created_at DESC LIMIT 1",
                (COMPANY,),
            ).fetchone()
            check(
                "b-success-confirm-audit",
                reply_b == (
                    "Done — receipt sent to HR: Sharma General Store, "
                    "₹1,250, 2026-11-02."
                )
                and len(sent) == 1
                and sent[0][0] == "hr@example.com"
                and "Sharma General Store" in sent[0][2]
                and "₹1,250" in sent[0][2]
                and "2026-11-02" in sent[0][2]
                and audit is not None
                and audit[0] == "sent_gmail",
                f"reply={reply_b} audit={audit}",
            )

            # (c) Unreadable: no transcription, Ollama unreachable.
            sent.clear()
            os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"
            try:
                reply_c = receipts_mod.process_receipt(
                    conn, COMPANY, "910000000401", "employee", "",
                    b"fake-bytes", "image/jpeg",
                )
            finally:
                os.environ.pop("OLLAMA_URL", None)
            check(
                "c-unreadable-no-send",
                reply_c == receipts_mod.COULD_NOT_READ_REPLY
                and sent == [],
                f"reply={reply_c}",
            )

            # (d) Ollama fallback plumbing via a stub server.
            hits = []

            class _Stub(BaseHTTPRequestHandler):
                def do_POST(self):
                    length = int(self.headers.get("Content-Length", 0))
                    self.rfile.read(length)
                    hits.append(1)
                    payload = json.dumps(
                        {"message": {"content": SAMPLE}}
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def log_message(self, *args):
                    pass

            server = HTTPServer(("127.0.0.1", 0), _Stub)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            sent.clear()
            os.environ["OLLAMA_URL"] = f"http://127.0.0.1:{port}"
            try:
                reply_d = receipts_mod.process_receipt(
                    conn, COMPANY, "910000000401", "employee", "",
                    b"fake-bytes", "image/jpeg",
                )
            finally:
                os.environ.pop("OLLAMA_URL", None)
                server.shutdown()
            check(
                "d-ollama-fallback",
                reply_d.startswith("Done — receipt sent to HR:")
                and len(hits) >= 1
                and len(sent) == 1,
                f"reply={reply_d} hits={len(hits)}",
            )
    finally:
        gmail_api_mod.is_gmail_ready = real_ready
        gmail_api_mod.send_via_gmail = real_send
        gmail_api_mod.load_recipients = real_load

    # (e)+(f) Simulated WhatsApp round-trips.
    import app.llm as llm_mod
    import app.whatsapp as wa_mod

    real_transcribe = llm_mod.transcribe_image
    real_download = wa_mod._download_media
    real_deliver = wa_mod._deliver
    gmail_api_mod.is_gmail_ready = lambda: True
    gmail_api_mod.load_recipients = lambda: {"DEFAULT": "hr@example.com"}
    gmail_api_mod.send_via_gmail = _capture_send
    llm_mod.transcribe_image = lambda data, mime: SAMPLE

    async def _fake_download(token, media_id):
        return b"fake-bytes", "image/jpeg"

    wa_mod._download_media = _fake_download
    sent_bodies = []

    async def _fake_deliver(client, phone_id, token, sender_to, body):
        sent_bodies.append(body)
        return "sent", "fake-msg-id"

    wa_mod._deliver = _fake_deliver
    try:
        from fastapi.testclient import TestClient

        from app.main import app

        os.environ["COMPANY_ID"] = COMPANY
        os.environ["WHATSAPP_TOKEN"] = "dummy-token"
        os.environ["WHATSAPP_PHONE_ID"] = "12345"
        os.environ["MEDIA_DIR"] = tempfile.mkdtemp(prefix="wa_receipt_")
        client = TestClient(app)

        def _post_image(mid, caption):
            image = {"id": "media-" + mid, "mime_type": "image/jpeg"}
            if caption is not None:
                image["caption"] = caption
            payload = {
                "entry": [{
                    "changes": [{
                        "value": {
                            "messages": [{
                                "from": "910000000401",
                                "id": mid,
                                "timestamp": "1700000000",
                                "type": "image",
                                "image": image,
                            }]
                        }
                    }]
                }]
            }
            return client.post("/webhook/whatsapp", json=payload)

        def _wait_body():
            deadline = time.time() + 10.0
            while time.time() < deadline:
                if sent_bodies:
                    return sent_bodies[-1]
                time.sleep(0.2)
            return ""

        sent.clear()
        sent_bodies.clear()
        resp_e = _post_image("wamid-receipt-1", "receipt")
        body_e = _wait_body()
        check(
            "e-webhook-receipt",
            resp_e.status_code == 200
            and body_e.startswith("Done — receipt sent to HR:")
            and "Sharma General Store" in body_e
            and len(sent) == 1,
            f"body={body_e[:120]}",
        )

        sent.clear()
        sent_bodies.clear()
        resp_f = _post_image("wamid-receipt-2", None)
        body_f = _wait_body()
        check(
            "f-uncaptioned-no-email",
            resp_f.status_code == 200
            and "receipt sent to HR" not in body_f
            and sent == [],
            f"body={body_f[:120]}",
        )
    finally:
        llm_mod.transcribe_image = real_transcribe
        wa_mod._download_media = real_download
        wa_mod._deliver = real_deliver
        gmail_api_mod.is_gmail_ready = real_ready
        gmail_api_mod.send_via_gmail = real_send
        gmail_api_mod.load_recipients = real_load
        os.environ.pop("WHATSAPP_TOKEN", None)
        os.environ.pop("WHATSAPP_PHONE_ID", None)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
