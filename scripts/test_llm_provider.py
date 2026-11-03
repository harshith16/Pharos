"""Targeted proofs for the LLM provider switch (P1-6).

Prerequisites: none beyond the repo (no DB, no key, no Ollama).
A local stub server speaks Ollama's OpenAI-compatible endpoint
(POST /v1/chat/completions) so the ollama path is proven end to
end at code level: selection, prompt plumbing, local-endpoint
calls, and zero Meta calls (the base URL points at the stub, so
no Meta host is ever contacted). A live local Llama model run
remains a founder-PC check (his PC already has Ollama): pull the
model and run one query with LLM_PROVIDER=ollama.

Checks:
(a) provider normalisation: ollama/OLLAMA -> ollama, meta/empty/
    bogus/None -> meta.
(b) meta defaults: provider meta, base on the Meta host, model
    muse-spark-1.3 (or the configured META_MODEL).
(c) ollama synthesize through the stub: answer returned, request
    hit /v1/chat/completions with the Ollama chat model.
(d) ollama extract_facts through the stub: parsed fact returned.
(e) ollama compose_email through the stub: email text returned.
(f) ollama vision uses OLLAMA_VISION_MODEL, not the chat model.
(g) ollama unreachable -> synthesize returns '' gracefully (no
    exception, no fallback to Meta).
(h) structural zero-Meta: with provider ollama, _api_base()
    contains no Meta host.

Run: PYTHONPATH=. python scripts/test_llm_provider.py
Exit code 0 iff every check passes.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.llm as llm_mod

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))


def main():
    # (a) Normalisation.
    check(
        "a-normalize",
        llm_mod._normalize_provider("ollama") == "ollama"
        and llm_mod._normalize_provider("OLLAMA") == "ollama"
        and llm_mod._normalize_provider("meta") == "meta"
        and llm_mod._normalize_provider("") == "meta"
        and llm_mod._normalize_provider("bogus") == "meta"
        and llm_mod._normalize_provider(None) == "meta",
        "normalize ok",
    )

    orig_provider = llm_mod.PROVIDER
    orig_env = {
        key: os.environ.get(key)
        for key in ("OLLAMA_URL", "OLLAMA_MODEL", "OLLAMA_VISION_MODEL")
    }

    def _restore_env():
        for key, value in orig_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    try:
        # (b) Meta defaults (this process imported llm without
        # LLM_PROVIDER set, so PROVIDER must be meta).
        check(
            "b-meta-defaults",
            orig_provider == "meta"
            and "meta.ai" in llm_mod._api_base()
            and llm_mod._api_model() != "",
            f"provider={orig_provider} base={llm_mod._api_base()}",
        )

        # Stub Ollama server.
        captured = []

        class _Stub(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    payload = {}
                captured.append((self.path, payload))
                messages = payload.get("messages") or []
                system_text = ""
                user_text = ""
                multimodal = False
                for msg in messages:
                    content = msg.get("content")
                    if msg.get("role") == "system" and isinstance(content, str):
                        system_text = content
                    if isinstance(content, list):
                        multimodal = True
                    elif isinstance(content, str) and msg.get("role") == "user":
                        user_text = content
                if "Extract facts" in user_text or "Extract atomic" in system_text:
                    content = json.dumps({
                        "facts": [{
                            "fact": "Travel limit is 12000 rupees per trip.",
                            "visible_to": ["employee", "cfo", "cto", "ceo"],
                            "effective_date": None,
                            "triples": [{
                                "source": "travel_policy",
                                "relationship": "limit",
                                "destination": "12000",
                            }],
                        }]
                    })
                elif "Answer ONLY from the supplied facts" in system_text:
                    content = json.dumps({
                        "answer": (
                            "The travel limit is 12000 rupees per trip."
                        )
                    })
                elif multimodal:
                    content = "Stub transcription: Sharma General Store TOTAL Rs. 1,250"
                else:
                    content = "Stub email body for the request."
                body = json.dumps({
                    "choices": [{"message": {"content": content}}]
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), _Stub)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            llm_mod.PROVIDER = "ollama"
            os.environ["OLLAMA_URL"] = f"http://127.0.0.1:{port}"
            os.environ["OLLAMA_MODEL"] = "llama3.2:3b"
            os.environ["OLLAMA_VISION_MODEL"] = "test-vision-model"

            # (h) Structural zero-Meta under ollama.
            check(
                "h-zero-meta-base",
                "meta.ai" not in llm_mod._api_base()
                and llm_mod._api_base() == f"http://127.0.0.1:{port}/v1",
                f"base={llm_mod._api_base()}",
            )

            # (c) Synthesize through the stub.
            captured.clear()
            answer = llm_mod.synthesize(
                "What is the travel limit?",
                [{"id": "abc123",
                  "fact": "Travel limit is 12000 rupees per trip."}],
                [],
            )
            check(
                "c-ollama-synthesize",
                answer == "The travel limit is 12000 rupees per trip."
                and len(captured) == 1
                and captured[0][0] == "/v1/chat/completions"
                and captured[0][1].get("model") == "llama3.2:3b",
                f"answer={answer} model={captured[0][1].get('model') if captured else None}",
            )

            # (d) Extract facts through the stub.
            captured.clear()
            facts = llm_mod.extract_facts(
                "Travel limit is 12000 rupees per trip."
            )
            check(
                "d-ollama-extract",
                len(facts) == 1
                and facts[0]["fact"] == "Travel limit is 12000 rupees per trip."
                and len(captured) == 1
                and captured[0][1].get("model") == "llama3.2:3b",
                f"facts={facts}",
            )

            # (e) Compose email through the stub.
            captured.clear()
            email = llm_mod.compose_email(
                "HR", "reimbursement",
                [{"fact": "Travel limit is 12000 rupees per trip."}],
            )
            check(
                "e-ollama-compose",
                email.strip() == "Stub email body for the request."
                and len(captured) == 1
                and captured[0][1].get("model") == "llama3.2:3b",
                f"email={email}",
            )

            # (f) Vision uses the vision model, not the chat model.
            captured.clear()
            text = llm_mod.transcribe_image(b"fake-bytes", "image/jpeg")
            check(
                "f-ollama-vision-model",
                text is not None
                and "Stub transcription" in text
                and len(captured) == 1
                and captured[0][1].get("model") == "test-vision-model",
                f"text={text} model={captured[0][1].get('model') if captured else None}",
            )
        finally:
            server.shutdown()

        # (g) Ollama unreachable -> graceful '' (no Meta fallback).
        llm_mod.PROVIDER = "ollama"
        os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"
        os.environ["OLLAMA_MODEL"] = "llama3.2:3b"
        try:
            answer_g = llm_mod.synthesize(
                "What is the travel limit?",
                [{"id": "abc123",
                  "fact": "Travel limit is 12000 rupees per trip."}],
                [],
            )
        except Exception as exc:
            answer_g = f"RAISED {exc}"
        check(
            "g-ollama-down-graceful",
            answer_g == "",
            f"answer={answer_g!r}",
        )
    finally:
        llm_mod.PROVIDER = orig_provider
        _restore_env()

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
