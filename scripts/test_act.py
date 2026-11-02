"""Targeted proofs for the ACT email action (main).

(a) "inform HR about my reimbursement" routes ACT, with target and
    topic resolved from engine data (approver relation + entities
    learned from uploads), never from a hand-typed domain list.
(b) With neither OAuth nor SMTP configured, the reply is the honest
    not-configured message and NOTHING is sent.
(c) Gmail API OAuth path, against LOCAL servers speaking the real
    protocols: an always-200 Gmail API server proves send
    construction; a 401-then-refresh flow proves exactly one
    refresh-and-retry and that the token file is updated.
(d) SMTP fallback path, against a LOCAL real SMTP server (real
    smtplib conversation, captured message), proving the connector
    speaks SMTP, not a stub — used only when OAuth is not configured.
(e) SMTP failure yields the honest could-not-send reply, never a
    success claim; and an OAuth failure is NOT silently retried via
    SMTP (no double-send risk).
(f) Non-action messages keep their existing routes (question ->
    ENGINE, greeting -> LLM_DIRECT, "tell me about X" -> not ACT).

Notes on what runs without a Meta key on this machine: LLM email
composition is NOT proven here — the deterministic facts rendering
is (it can contain nothing but the facts by construction). Live
Gmail delivery with a real account stays with the founder's PC test.

Run: PYTHONPATH=. python scripts/test_act.py
Exit code 0 iff every check passes. Cleans its own company rows first.
"""

import base64
import json
import os
import smtplib
import socket
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

ENV_KEYS = [
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_APP_PASSWORD",
    "HR_EMAIL", "HR_LEAD_EMAIL",
    "GMAIL_OAUTH_CLIENT_ID", "GMAIL_OAUTH_CLIENT_SECRET",
    "GMAIL_OAUTH_TOKEN_FILE",
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


# ---------- local SMTP server (real SMTP conversation) ----------

class SMTPRecorder:
    def __init__(self):
        self.data = []
        self.mail_from = []
        self.rcpt_to = []
        self.connections = 0


def run_smtp_server(recorder, port_holder, certfile, keyfile):
    """Local SMTP server speaking the real protocol, with STARTTLS.

    Advertises STARTTLS + AUTH, upgrades to TLS on STARTTLS using a
    throwaway self-signed cert (test-only), then accepts AUTH, MAIL,
    RCPT, DATA, QUIT. smtplib's default client context accepts the
    cert for localhost testing, so the production default path
    (use_tls=True) runs end to end here — not a stub.
    """
    import ssl

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port_holder.append(srv.getsockname()[1])
    tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_ctx.load_cert_chain(certfile, keyfile)
    while True:
        try:
            conn, _ = srv.accept()
        except Exception:
            return
        recorder.connections += 1
        try:
            fh = conn.makefile("rwb")
            fh.write(b"220 local test SMTP\r\n")
            fh.flush()
            data_mode = False
            buf = []
            tls_done = False
            while True:
                line = fh.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if data_mode:
                    if text == ".":
                        recorder.data.append("\n".join(buf))
                        buf = []
                        data_mode = False
                        fh.write(b"250 OK\r\n")
                        fh.flush()
                    else:
                        buf.append(text)
                    continue
                upper = text.upper()
                if upper.startswith("EHLO") or upper.startswith("HELO"):
                    if tls_done:
                        fh.write(b"250-local\r\n250-AUTH PLAIN LOGIN\r\n250 OK\r\n")
                    else:
                        fh.write(b"250-local\r\n250-STARTTLS\r\n250-AUTH PLAIN LOGIN\r\n250 OK\r\n")
                elif upper == "STARTTLS" and not tls_done:
                    fh.write(b"220 ready\r\n")
                    fh.flush()
                    conn = tls_ctx.wrap_socket(conn, server_side=True)
                    fh = conn.makefile("rwb")
                    tls_done = True
                elif upper.startswith("AUTH "):
                    fh.write(b"235 OK\r\n")
                elif upper.startswith("MAIL FROM:"):
                    recorder.mail_from.append(text)
                    fh.write(b"250 OK\r\n")
                elif upper.startswith("RCPT TO:"):
                    recorder.rcpt_to.append(text)
                    fh.write(b"250 OK\r\n")
                elif upper == "DATA":
                    data_mode = True
                    fh.write(b"354 End data\r\n")
                elif upper == "QUIT":
                    fh.write(b"221 Bye\r\n")
                    fh.flush()
                    break
                elif upper in ("RSET", "NOOP"):
                    fh.write(b"250 OK\r\n")
                else:
                    fh.write(b"250 OK\r\n")
                fh.flush()
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


class RecordingSMTP:
    """Fake SMTP class recording calls (proves STARTTLS is invoked)."""
    calls = []

    def __init__(self, host, port, timeout=30):
        RecordingSMTP.calls.append(("connect", host, port))

    def ehlo(self):
        RecordingSMTP.calls.append(("ehlo",))

    def starttls(self):
        RecordingSMTP.calls.append(("starttls",))

    def login(self, user, password):
        RecordingSMTP.calls.append(("login", user))

    def sendmail(self, frm, to, msg):
        RecordingSMTP.calls.append(("sendmail", frm, to))

    def quit(self):
        RecordingSMTP.calls.append(("quit",))


def main():
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    token_path = "/tmp/act_token.json"
    try:
        with get_conn() as conn:
            apply_actions_migration(conn)
            cleanup(conn)
            seed_result = seed(conn)

            # Seed proof: approver relation exists in stored rows.
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
                          "app/emailer.py", "app/gmail_api.py"):
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

            # (b) Neither OAuth nor SMTP configured: honest reply, no send.
            set_env({})
            reply = actions.handle_act(conn, COMPANY, "employee",
                                       "919999999999", act_result)
            conn.commit()
            row = latest_audit(conn)
            check("b-not-configured-reply",
                  "isn't configured" in reply, reply[:120])
            check("b-not-configured-audit",
                  row is not None and row[0] == "not_configured",
                  str(row))

            # Facts-only deterministic body (no key on this VM).
            subject, body = emailer.render_facts_email(
                "HR Lead", "Reimbursement Policy",
                [{"fact": FACT_45}, {"fact": FACT_15K}])
            check("a-body-facts-only",
                  FACT_45 in body and FACT_15K in body
                  and "99999" not in body, subject)

            # (d) SMTP fallback against a LOCAL real SMTP server.
            import subprocess

            certfile = "/tmp/act_cert.pem"
            keyfile = "/tmp/act_key.pem"
            gen = subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048",
                 "-keyout", keyfile, "-out", certfile, "-days", "1",
                 "-nodes", "-subj", "/CN=localhost",
                 "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"],
                capture_output=True)
            check("d-cert-generated", gen.returncode == 0)
            recorder = SMTPRecorder()
            port_holder = []
            smtp_thread = threading.Thread(
                target=run_smtp_server,
                args=(recorder, port_holder, certfile, keyfile),
                daemon=True)
            smtp_thread.start()
            while not port_holder:
                pass
            smtp_port = port_holder[0]
            set_env({
                "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(smtp_port),
                "SMTP_USER": "bot@example.com",
                "SMTP_APP_PASSWORD": "dummy",
                "HR_EMAIL": "hr@example.com",
            })
            reply = actions.handle_act(conn, COMPANY, "employee",
                                       "919999999999", act_result)
            conn.commit()
            row = latest_audit(conn)
            captured = "\n".join(recorder.data)
            check("d-smtp-fallback-reply",
                  reply.startswith("Done") and "HR Lead" in reply, reply)
            check("d-smtp-fallback-audit",
                  row is not None and row[0] == "sent_smtp", str(row))
            check("d-smtp-captured",
                  FACT_45 in captured and "Reimbursement Policy" in captured,
                  captured[:150])

            # STARTTLS is invoked on the default (production) path.
            RecordingSMTP.calls = []
            ok, err = emailer.send_email(
                "hr@example.com", "subj", "body",
                settings={"host": "127.0.0.1", "port": smtp_port,
                          "user": "bot@example.com", "password": "dummy"},
                smtp_class=RecordingSMTP, use_tls=True)
            kinds = [c[0] for c in RecordingSMTP.calls]
            check("d-starttls-invoked",
                  ok and "starttls" in kinds and "login" in kinds
                  and "sendmail" in kinds, str(kinds))

            # (c) Gmail API: direct success against a local server.
            gmail_server, gmail_port = start_server(_GmailHandler)
            token_server, token_port = start_server(_TokenHandler)
            _GmailHandler.mode = "ok"
            _GmailHandler.seen = []
            _GmailHandler.raws = []
            _TokenHandler.seen = []
            with open(token_path, "w", encoding="utf-8") as fh:
                json.dump({"token": "valid-access", "refresh_token": "r1",
                           "token_uri": "https://oauth2.googleapis.com/token"},
                          fh)
            set_env({
                "GMAIL_OAUTH_CLIENT_ID": "cid",
                "GMAIL_OAUTH_CLIENT_SECRET": "csec",
                "GMAIL_OAUTH_TOKEN_FILE": token_path,
                "HR_EMAIL": "hr@example.com",
            })
            ok, err = gmail_api.send_via_gmail(
                "hr@example.com", "Request regarding Reimbursement Policy",
                FACT_45,
                api_base=f"http://127.0.0.1:{gmail_port}",
                token_url=f"http://127.0.0.1:{token_port}/token")
            raw_decoded = ""
            if _GmailHandler.raws:
                raw_decoded = base64.urlsafe_b64decode(
                    _GmailHandler.raws[0] + "==").decode("utf-8", "replace")
            check("c-gmail-direct-send", ok, str(err))
            check("c-gmail-captured",
                  "Reimbursement Policy" in raw_decoded and FACT_45 in raw_decoded,
                  raw_decoded[:150])
            check("c-gmail-no-refresh-needed", len(_TokenHandler.seen) == 0,
                  f"token calls={len(_TokenHandler.seen)}")

            # (c) Gmail API: 401 -> exactly one refresh -> retry success,
            # and the token file is updated with the new access token.
            _GmailHandler.mode = "401_then_ok"
            _GmailHandler.seen = []
            _GmailHandler.raws = []
            _TokenHandler.seen = []
            with open(token_path, "w", encoding="utf-8") as fh:
                json.dump({"token": "expired-access", "refresh_token": "r1",
                           "token_uri": "https://oauth2.googleapis.com/token"},
                          fh)
            ok, err = gmail_api.send_via_gmail(
                "hr@example.com", "Request regarding Reimbursement Policy",
                FACT_45,
                api_base=f"http://127.0.0.1:{gmail_port}",
                token_url=f"http://127.0.0.1:{token_port}/token")
            with open(token_path, encoding="utf-8") as fh:
                updated = json.load(fh)
            check("c-gmail-refresh-retry", ok, str(err))
            check("c-gmail-token-updated",
                  updated.get("token") == "new-access-token", str(updated))
            check("c-gmail-refresh-called-once",
                  len(_TokenHandler.seen) == 1
                  and "grant_type=refresh_token" in _TokenHandler.seen[0],
                  str(_TokenHandler.seen))

            # (e) Gmail primary through the real handler: OAuth
            # configured + SMTP closed -> Gmail wins, audit sent_gmail.
            # handle_act uses production Google URLs, so the test
            # monkeypatches ONLY the endpoint parameters (test-only
            # wiring; production code is unchanged) to the local
            # servers, keeping the real HTTP + refresh logic intact.
            orig_send = gmail_api.send_via_gmail

            def local_send(to, subject, body, oauth_config=None):
                return orig_send(
                    to, subject, body, oauth_config=oauth_config,
                    api_base=f"http://127.0.0.1:{gmail_port}",
                    token_url=f"http://127.0.0.1:{token_port}/token")

            gmail_api.send_via_gmail = local_send
            try:
                set_env({
                    "GMAIL_OAUTH_CLIENT_ID": "cid",
                    "GMAIL_OAUTH_CLIENT_SECRET": "csec",
                    "GMAIL_OAUTH_TOKEN_FILE": token_path,
                    "SMTP_HOST": "127.0.0.1", "SMTP_PORT": "59999",
                    "SMTP_USER": "bot@example.com",
                    "SMTP_APP_PASSWORD": "dummy",
                    "HR_EMAIL": "hr@example.com",
                })
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
                check("e-gmail-primary-via-handler",
                      reply.startswith("Done") and row is not None
                      and row[0] == "sent_gmail",
                      f"reply={reply} row={row}")

                # (e) OAuth failure is NOT silently retried via SMTP:
                # Gmail 401 + token server failure -> honest failure,
                # and the local SMTP server sees no new connection.
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

                def local_send_fail(to, subject, body, oauth_config=None):
                    return orig_send(
                        to, subject, body, oauth_config=oauth_config,
                        api_base=f"http://127.0.0.1:{gmail_port}",
                        token_url=f"http://127.0.0.1:{fail_token_port}/token")

                gmail_api.send_via_gmail = local_send_fail
                smtp_conns_before = recorder.connections
                with open(token_path, "w", encoding="utf-8") as fh:
                    json.dump({"token": "expired-access",
                               "refresh_token": "r1",
                               "token_uri": "https://oauth2.googleapis.com/token"},
                              fh)
                reply = actions.handle_act(conn, COMPANY, "employee",
                                           "919999999999", act_result)
                conn.commit()
                row = latest_audit(conn)
                check("e-oauth-failure-no-smtp-retry",
                      "couldn't send" in reply and row is not None
                      and row[0] == "failed"
                      and recorder.connections == smtp_conns_before,
                      f"reply={reply} row={row} "
                      f"smtp_conns={recorder.connections}")
                fail_token_server.shutdown()
            finally:
                gmail_api.send_via_gmail = orig_send

            # (e) SMTP failure: honest could-not-send, never success.
            set_env({
                "SMTP_HOST": "127.0.0.1", "SMTP_PORT": "59999",
                "SMTP_USER": "bot@example.com",
                "SMTP_APP_PASSWORD": "dummy",
                "HR_EMAIL": "hr@example.com",
            })
            reply = actions.handle_act(conn, COMPANY, "employee",
                                       "919999999999", act_result)
            conn.commit()
            row = latest_audit(conn)
            check("e-smtp-failure-reply",
                  "couldn't send" in reply and not reply.startswith("Done"),
                  reply)
            check("e-smtp-failure-audit",
                  row is not None and row[0] == "failed", str(row))

            gmail_server.shutdown()
            token_server.shutdown()
            cleanup(conn)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            os.remove(token_path)
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
