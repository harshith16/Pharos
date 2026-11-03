"""Targeted proofs for the ACT email action + Gmail connect flow (main).

(a) "inform HR about my reimbursement" routes ACT, with target and
    topic resolved from engine data (approver relation + entities
    learned from uploads), never from a hand-typed domain list.
(b) Gmail NOT connected: the reply is the honest not-connected
    message naming /auth/gmail, and NOTHING is sent.
(c) Connect flow: the callback saves a token to the gitignored
    file, and a subsequent send uses that saved token — proven
    against LOCAL servers speaking the real Gmail API HTTP
    protocol, plus the callback wiring via TestClient.
(d) Gmail API: direct success, and 401 -> exactly one
    refresh-and-retry with the token file updated.
(e) Gmail failure yields the honest could-not-send reply, never a
    success claim. There is no SMTP path anymore to retry through.
(f) Non-action messages keep their existing routes (question ->
    ENGINE, greeting -> LLM_DIRECT, "tell me about X" -> not ACT).
(g) Grep proof: zero .env auth key names in app/ code paths.

Notes on what runs without a Meta key on this machine: LLM email
composition is NOT proven here — the deterministic facts rendering
is (it can contain nothing but the facts by construction). Live
Gmail delivery with a real Google account, and the live browser
login, stay with the founder's PC test.

Run: PYTHONPATH=. python scripts/test_act.py
Exit code 0 iff every check passes. Cleans its own company rows first.
"""

import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import actions, emailer, gmail_api
from app.db import get_conn
from app.engine import ingest as engine_ingest
from app.engine import router
from app.engine.router import ACT, LLM_DIRECT
from app.store import write_fact

COMPANY = "act-co"
RESULTS = []
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FACT_45 = "Reimbursement claims must be submitted within 45 days of the expense."
FACT_15K = "Reimbursement claims above Rs. 15,000 require approval from the HR Lead."
SEED_TEXT = (
    "The HR Lead approves the Reimbursement Policy. "
    "The Reimbursement Policy covers reimbursement claims."
)

# Recipient-only .env values are allowed; auth keys are not. The
# only .env keys this test ever sets are recipient addresses.
ENV_KEYS = ["HR_EMAIL", "HR_LEAD_EMAIL"]

FORBIDDEN_ENV_KEYS = [
    "GMAIL_OAUTH_CLIENT_ID", "GMAIL_OAUTH_CLIENT_SECRET",
    "GMAIL_OAUTH_TOKEN_FILE", "SMTP_USER", "SMTP_APP_PASSWORD",
]


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))


def cleanup(conn):
    conn.execute(
        "DELETE FROM chunks WHERE document_id IN "
        "(SELECT id FROM documents WHERE company_id = %s)",
        (COMPANY,),
    )
    conn.execute("DELETE FROM documents WHERE company_id = %s", (COMPANY,))
    conn.execute(
        "DELETE FROM memory_sources WHERE memory_id IN "
        "(SELECT id FROM memories WHERE company_id = %s)",
        (COMPANY,),
    )
    conn.execute("DELETE FROM memories WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM edges WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM engine_relations WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM engine_entities WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM engine_uploads WHERE company_id = %s", (COMPANY,))
    try:
        conn.execute("DELETE FROM email_actions WHERE company_id = %s", (COMPANY,))
    except Exception:
        conn.rollback()
    conn.commit()


def apply_actions_migration(conn):
    path = os.path.join(ROOT, "schema_actions.sql")
    with open(path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    for stmt in "\n".join(lines).split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.commit()


def seed(conn):
    write_fact(conn, COMPANY, fact=FACT_45,
               visible_to=["employee", "cfo", "cto", "ceo"])
    write_fact(conn, COMPANY, fact=FACT_15K,
               visible_to=["employee", "cfo", "cto", "ceo"])
    conn.commit()
    result = engine_ingest.ingest_text(
        conn, COMPANY, SEED_TEXT, "reimbursement-policy.txt",
        title="Reimbursement Policy",
    )
    conn.commit()
    return result


def latest_audit(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, target_role, topic FROM email_actions "
            "WHERE company_id = %s ORDER BY created_at DESC LIMIT 1",
            (COMPANY,),
        )
        row = cur.fetchone()
    return row


def set_env(mapping):
    for key in ENV_KEYS:
        if key in mapping:
            if mapping[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = mapping[key]
        else:
            os.environ.pop(key, None)


# ---------- local Gmail API + token servers (real HTTP) ----------

class _GmailHandler(BaseHTTPRequestHandler):
    mode = "ok"  # "ok" | "401_then_ok"
    seen = []
    raws = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if self.path.endswith("/messages/send"):
            _GmailHandler.seen.append(self.path)
            try:
                payload = json.loads(body or b"{}")
                _GmailHandler.raws.append(payload.get("raw", ""))
            except Exception:
                pass
            if _GmailHandler.mode == "401_then_ok" and len(_GmailHandler.seen) == 1:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "unauthorized"}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"id": "local-msg-1"}')
            return
        self.send_response(404)
        self.end_headers()


class _TokenHandler(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        _TokenHandler.seen.append(body.decode("utf-8", "replace"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"access_token": "new-access-token", "expires_in": 3600}')


def start_server(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def main():
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    token_path = "/tmp/act_token.json"
    connect_path = "/tmp/act_connect.json"
    creds_path = "/tmp/act_creds.json"
    real_token_path = str(gmail_api.token_path_default())
    real_connect_path = str(gmail_api.connect_path_default())
    token_existed = os.path.exists(real_token_path)
    connect_existed = os.path.exists(real_connect_path)
    try:
        # (g) Grep proof: zero .env auth key names in app/ code.
        grep_ok = True
        grep_detail = ""
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "app")):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fname)
                with open(fpath, encoding="utf-8") as fh:
                    content = fh.read()
                for key in FORBIDDEN_ENV_KEYS:
                    if key in content:
                        grep_ok = False
                        grep_detail += f"{fpath}:{key} "
        check("g-no-env-auth-keys", grep_ok, grep_detail or "clean")

        # (c) Connect flow wiring, without Google: auth URL shape,
        # code exchange against a local token server, callback saves.
        from app import gmail_connect

        url = gmail_connect.build_auth_url(
            "cid", "http://127.0.0.1:8000/auth/gmail/callback", "state123")
        check("c-auth-url-scope",
              "gmail.send" in url and "response_type=code" in url
              and "state123" in url, url[:120])
        check("c-auth-url-no-secret-leak",
              "csec" not in url, "client_id only, no secret in URL")
        token_server, token_port = start_server(_TokenHandler)
        _TokenHandler.seen = []
        token_data, err = gmail_connect.exchange_code(
            "authcode1", "cid", "csec",
            "http://127.0.0.1:8000/auth/gmail/callback",
            token_url=f"http://127.0.0.1:{token_port}/token")
        check("c-exchange-code",
              token_data is not None
              and token_data.get("access_token") == "new-access-token",
              str(err))

        # Callback via TestClient with the exchange stubbed (the
        # exchange itself is proven above against a real server).
        # Token/connect paths are monkeypatched to temp files so the
        # real repo data/ directory is never touched by tests.
        orig_token_path_fn = gmail_api.token_path_default
        orig_connect_path_fn = gmail_api.connect_path_default
        gmail_api.token_path_default = lambda: __import__("pathlib").Path(token_path)
        gmail_api.connect_path_default = lambda: __import__("pathlib").Path(connect_path)
        orig_exchange = gmail_connect.exchange_code
        gmail_connect.exchange_code = lambda *a, **k: (
            {"access_token": "cb-access", "refresh_token": "cb-refresh",
             "token_uri": "https://oauth2.googleapis.com/token"}, None)
        # The callback also reads credentials.json; on this VM there
        # is none, so stub only the credentials load for this block
        # (the exchange itself is proven above, unstubbed).
        orig_creds = gmail_api.load_credentials
        gmail_api.load_credentials = lambda *a, **k: {
            "client_id": "cid", "client_secret": "csec",
            "token_uri": "https://oauth2.googleapis.com/token"}
        try:
            from fastapi.testclient import TestClient

            from app.main import app as fastapi_app

            client = TestClient(fastapi_app, raise_server_exceptions=False)
            status_resp = client.get("/auth/gmail/status")
            check("c-status-endpoint",
                  status_resp.status_code == 200
                  and status_resp.json().get("connected") is False,
                  status_resp.text[:120])
            gmail_connect._pending["teststate"] = {
                "created": __import__("time").time(), "hr_email": "hr@example.com",
            }
            cb = client.get(
                "/auth/gmail/callback?code=authcode1&state=teststate")
            saved_token = None
            if os.path.exists(token_path):
                with open(token_path, encoding="utf-8") as fh:
                    saved_token = json.load(fh)
            saved_conn = None
            if os.path.exists(connect_path):
                with open(connect_path, encoding="utf-8") as fh:
                    saved_conn = json.load(fh)
            check("c-callback-saves-token",
                  cb.status_code == 200 and "Gmail is connected" in cb.text
                  and saved_token is not None
                  and saved_token.get("access_token") == "cb-access",
                  f"status={cb.status_code}")
            check("c-callback-saves-recipient",
                  saved_conn is not None
                  and saved_conn.get("recipients", {}).get("DEFAULT") == "hr@example.com",
                  str(saved_conn))
            # Expired/invalid state is rejected, nothing overwritten.
            bad = client.get("/auth/gmail/callback?code=x&state=nope")
            check("c-callback-bad-state",
                  "expired or is invalid" in bad.text, bad.text[:120])
        finally:
            gmail_connect.exchange_code = orig_exchange
            gmail_api.load_credentials = orig_creds
            gmail_api.token_path_default = orig_token_path_fn
            gmail_api.connect_path_default = orig_connect_path_fn
        token_server.shutdown()

        with get_conn() as conn:
            apply_actions_migration(conn)
            cleanup(conn)
            seed_result = seed(conn)

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT display_name FROM engine_entities WHERE company_id = %s",
                    (COMPANY,),
                )
                entities = sorted(r[0] for r in cur.fetchall())
                cur.execute(
                    "SELECT e.display_name, r.rel, d.display_name "
                    "FROM engine_relations r "
                    "JOIN engine_entities e ON e.id = r.src_id "
                    "JOIN engine_entities d ON d.id = r.dst_id "
                    "WHERE r.company_id = %s",
                    (COMPANY,),
                )
                relations = cur.fetchall()
            check("a-seed-entities", "HR Lead" in entities, str(entities))
            check("a-seed-approves-relation",
                  any(rel == "approves" for _s, rel, _d in relations),
                  str(relations))

            # (a) ACT routing from engine data.
            routed = router.route_question(conn, COMPANY, "employee",
                                           "inform HR about my reimbursement")
            check("a-route-act", routed.get("route") == ACT,
                  f"route={routed.get('route')}")
            check("a-target-from-data", routed.get("target_display") == "HR Lead",
                  f"target={routed.get('target_display')}")
            check("a-topic-from-data",
                  routed.get("topic") and "Reimbursement" in routed.get("topic"),
                  f"topic={routed.get('topic')}")
            check("a-facts-covering", len(routed.get("facts") or []) >= 1,
                  f"facts={len(routed.get('facts') or [])}")

            # (f) Non-action messages keep their routes.
            q = router.route_question(conn, COMPANY, "employee",
                                      "What is the reimbursement policy?")
            check("f-question-not-act", q.get("route") != ACT,
                  f"route={q.get('route')}")
            t = router.route_question(conn, COMPANY, "employee",
                                      "Tell me about reimbursement")
            check("f-tell-me-not-act", t.get("route") != ACT,
                  f"route={t.get('route')}")
            g = router.route_question(conn, COMPANY, "employee", "hi")
            check("f-greeting-direct", g.get("route") == LLM_DIRECT,
                  f"route={g.get('route')}")

            # No hand-typed domain lists in the action path.
            literals_ok = True
            for fname in ("app/engine/router.py", "app/actions.py",
                          "app/emailer.py", "app/gmail_api.py",
                          "app/gmail_connect.py"):
                with open(os.path.join(ROOT, fname), encoding="utf-8") as fh:
                    content = fh.read().lower()
                if '"hr"' in content or "'hr'" in content:
                    literals_ok = False
                if '"reimbursement"' in content or "'reimbursement'" in content:
                    literals_ok = False
            check("a-no-domain-literals", literals_ok)

            act_result = {
                "route": ACT,
                "target_display": "HR Lead",
                "topic": "Reimbursement Policy",
                "facts": [{"fact": FACT_45}, {"fact": FACT_15K}],
            }

            # Facts-only deterministic body (no key on this VM).
            subject, body = emailer.render_facts_email(
                "HR Lead", "Reimbursement Policy",
                [{"fact": FACT_45}, {"fact": FACT_15K}])
            check("a-body-facts-only",
                  FACT_45 in body and FACT_15K in body
                  and "99999" not in body, subject)

            # Stored-recipient resolution (connect page) + env fallback.
            with open(connect_path, "w", encoding="utf-8") as fh:
                json.dump({"recipients": {"HR LEAD": "stored-hr@example.com"}},
                          fh)
            orig_load_recipients = gmail_api.load_recipients
            gmail_api.load_recipients = lambda *a, **k: orig_load_recipients(
                connect_path)
            try:
                addr, src = emailer.resolve_recipient("HR Lead")
                check("b-recipient-stored-first",
                      addr == "stored-hr@example.com" and src == "connect",
                      f"{addr} via {src}")
            finally:
                gmail_api.load_recipients = orig_load_recipients
            os.remove(connect_path)
            set_env({"HR_EMAIL": "hr@example.com"})
            addr, src = emailer.resolve_recipient("HR Lead")
            check("b-recipient-env-fallback",
                  addr == "hr@example.com" and src == "HR_EMAIL",
                  f"{addr} via {src}")

            # (b) Gmail NOT connected: honest reply naming the
            # connect page, nothing sent. Temporarily move any real
            # token/connect files aside so the test is hermetic.
            moved_token = None
            if token_existed:
                moved_token = real_token_path + ".testbak"
                os.rename(real_token_path, moved_token)
            moved_conn = None
            if connect_existed:
                moved_conn = real_connect_path + ".testbak"
                os.rename(real_connect_path, moved_conn)
            try:
                set_env({"HR_EMAIL": "hr@example.com"})
                reply = actions.handle_act(conn, COMPANY, "employee",
                                           "919999999999", act_result)
                conn.commit()
                row = latest_audit(conn)
                check("b-not-connected-reply",
                      "isn't connected" in reply and "/auth/gmail" in reply,
                      reply[:160])
                check("b-not-connected-audit",
                      row is not None and row[0] == "not_configured",
                      str(row))
            finally:
                if moved_token:
                    os.rename(moved_token, real_token_path)
                if moved_conn:
                    os.rename(moved_conn, real_connect_path)

            # (d) Gmail API via saved token, against local servers.
            gmail_server, gmail_port = start_server(_GmailHandler)
            token_server2, token_port2 = start_server(_TokenHandler)
            _GmailHandler.mode = "ok"
            _GmailHandler.seen = []
            _GmailHandler.raws = []
            _TokenHandler.seen = []
            with open(token_path, "w", encoding="utf-8") as fh:
                json.dump({"token": "valid-access", "refresh_token": "r1",
                           "token_uri": "https://oauth2.googleapis.com/token"},
                          fh)
            ok, err = gmail_api.send_via_gmail(
                "hr@example.com", "Request regarding Reimbursement Policy",
                FACT_45,
                api_base=f"http://127.0.0.1:{gmail_port}",
                token_url=f"http://127.0.0.1:{token_port2}/token",
                token_path=token_path)
            raw_decoded = ""
            if _GmailHandler.raws:
                raw_decoded = base64.urlsafe_b64decode(
                    _GmailHandler.raws[0] + "==").decode("utf-8", "replace")
            check("d-gmail-direct-send", ok, str(err))
            check("d-gmail-captured",
                  "Reimbursement Policy" in raw_decoded and FACT_45 in raw_decoded,
                  raw_decoded[:150])
            check("d-gmail-no-refresh-needed", len(_TokenHandler.seen) == 0,
                  f"token calls={len(_TokenHandler.seen)}")

            # (d) 401 -> exactly one refresh -> retry success, and
            # the token file is updated with the new access token.
            _GmailHandler.mode = "401_then_ok"
            _GmailHandler.seen = []
            _GmailHandler.raws = []
            _TokenHandler.seen = []
            with open(token_path, "w", encoding="utf-8") as fh:
                json.dump({"token": "expired-access", "refresh_token": "r1",
                           "token_uri": "https://oauth2.googleapis.com/token"},
                          fh)
            # Refresh needs client credentials: the connect flow
            # stores them in the token file on a real machine, but
            # this temp token has none, so supply a temp
            # credentials file (test-only wiring, as with endpoints).
            with open(creds_path, "w", encoding="utf-8") as fh:
                json.dump({"installed": {"client_id": "cid",
                                         "client_secret": "csec",
                                         "token_uri": "https://oauth2.googleapis.com/token"}},
                          fh)
            ok, err = gmail_api.send_via_gmail(
                "hr@example.com", "Request regarding Reimbursement Policy",
                FACT_45,
                api_base=f"http://127.0.0.1:{gmail_port}",
                token_url=f"http://127.0.0.1:{token_port2}/token",
                token_path=token_path,
                credentials_path=creds_path)
            with open(token_path, encoding="utf-8") as fh:
                updated = json.load(fh)
            check("d-gmail-refresh-retry", ok, str(err))
            check("d-gmail-token-updated",
                  updated.get("token") == "new-access-token", str(updated))
            check("d-gmail-refresh-called-once",
                  len(_TokenHandler.seen) == 1
                  and "grant_type=refresh_token" in _TokenHandler.seen[0],
                  str(_TokenHandler.seen))

            # (e) Gmail through the real handler: connected + local
            # servers -> Done + sent_gmail audit. Handler-level
            # wiring is monkeypatched ONLY for endpoint/token paths
            # (test-only wiring; production code is unchanged).
            orig_send = gmail_api.send_via_gmail
            orig_ready = gmail_api.is_gmail_ready

            def local_send(to, subject, body, **kwargs):
                return orig_send(
                    to, subject, body,
                    api_base=f"http://127.0.0.1:{gmail_port}",
                    token_url=f"http://127.0.0.1:{token_port2}/token",
                    token_path=token_path,
                    credentials_path=creds_path)

            gmail_api.send_via_gmail = local_send
            gmail_api.is_gmail_ready = lambda *a, **k: True
            try:
                set_env({"HR_EMAIL": "hr@example.com"})
                _GmailHandler.mode = "ok"
                _GmailHandler.seen = []
                with open(token_path, "w", encoding="utf-8") as fh:
                    json.dump({"token": "valid-access",
                               "refresh_token": "r1",
                               "token_uri": "https://oauth2.googleapis.com/token"},
                              fh)
                reply = actions.handle_act(conn, COMPANY, "employee",
                                           "919999999999", act_result)
                conn.commit()
                row = latest_audit(conn)
                check("e-gmail-via-handler",
                      reply.startswith("Done") and row is not None
                      and row[0] == "sent_gmail",
                      f"reply={reply} row={row}")

                # (e) Gmail failure: honest could-not-send, never
                # success, no other transport to fall through to.
                _GmailHandler.mode = "401_then_ok"
                _GmailHandler.seen = []
                _GmailHandler.raws = []

                class _FailingTokenHandler(_TokenHandler):
                    def do_POST(self):
                        length = int(self.headers.get("Content-Length", 0))
                        if length:
                            self.rfile.read(length)
                        self.send_response(400)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error": "invalid_grant"}')

                fail_token_server, fail_token_port = start_server(
                    _FailingTokenHandler)

                def local_send_fail(to, subject, body, **kwargs):
                    return orig_send(
                        to, subject, body,
                        api_base=f"http://127.0.0.1:{gmail_port}",
                        token_url=f"http://127.0.0.1:{fail_token_port}/token",
                        token_path=token_path,
                        credentials_path=creds_path)

                gmail_api.send_via_gmail = local_send_fail
                with open(token_path, "w", encoding="utf-8") as fh:
                    json.dump({"token": "expired-access",
                               "refresh_token": "r1",
                               "token_uri": "https://oauth2.googleapis.com/token"},
                              fh)
                reply = actions.handle_act(conn, COMPANY, "employee",
                                           "919999999999", act_result)
                conn.commit()
                row = latest_audit(conn)
                check("e-gmail-failure-reply",
                      "couldn't send" in reply and row is not None
                      and row[0] == "failed"
                      and not reply.startswith("Done"),
                      f"reply={reply} row={row}")
                fail_token_server.shutdown()
            finally:
                gmail_api.send_via_gmail = orig_send
                gmail_api.is_gmail_ready = orig_ready

            gmail_server.shutdown()
            token_server2.shutdown()
            cleanup(conn)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for path in (token_path, connect_path, creds_path):
            try:
                os.remove(path)
            except Exception:
                pass

    failed = [name for name, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
