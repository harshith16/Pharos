"""Regression proofs for the 23:09 small-talk crash (main).

A Meta API 200 with null message content crashed generate_smalltalk
with "'NoneType' object has no attribute 'strip'", and the greeting
got no reply. Every LLM reply path must now degrade instead: null or
empty content is logged and yields no reply for that turn — never a
crash.

No database and no real key are needed: httpx.post is stubbed, and
META_API_KEY is set to a clearly fake test value for the duration
(the value is not a secret and is never sent anywhere — the HTTP
call itself is stubbed).

Run: PYTHONPATH=. python scripts/test_llm_guards.py
Exit code 0 iff every check passes.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import llm

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _run(payload):
    """Run all four reply paths against one stubbed payload."""
    with patch("httpx.post", return_value=_FakeResp(payload)):
        return {
            "smalltalk": llm.generate_smalltalk("hi"),
            "answer": llm.generate_answer("hi"),
            "doc": llm.generate_doc_reply("policy.txt", "some text", 1),
            "image": llm.transcribe_image(b"fake-bytes", "image/jpeg"),
        }


def main():
    old_key = os.environ.get("META_API_KEY")
    os.environ["META_API_KEY"] = "dummy-test-key"
    try:
        # The exact 23:09 shape: 200, content null.
        out = _run({"choices": [{"message": {"content": None}}]})
        check("null-smalltalk-empty", out["smalltalk"] == "",
              f"got={out['smalltalk']!r}")
        check("null-answer-empty", out["answer"] == "",
              f"got={out['answer']!r}")
        check("null-doc-empty", out["doc"] == "",
              f"got={out['doc']!r}")
        check("null-image-none", out["image"] is None,
              f"got={out['image']!r}")

        # Empty choices and blank content degrade the same way.
        out = _run({"choices": []})
        check("empty-choices-smalltalk", out["smalltalk"] == "")
        check("empty-choices-image", out["image"] is None)
        out = _run({"choices": [{"message": {"content": "   "}}]})
        check("blank-smalltalk-empty", out["smalltalk"] == "")
        check("blank-image-none", out["image"] is None)

        # Normal content still flows through untouched.
        out = _run({"choices": [{"message": {"content": "Hello there!"}}]})
        check("normal-smalltalk", out["smalltalk"] == "Hello there!",
              f"got={out['smalltalk']!r}")
        check("normal-answer", out["answer"] == "Hello there!")
        check("normal-image", out["image"] == "Hello there!")

        # The JSON path rejects null content cleanly, never crashes.
        with patch("httpx.post", return_value=_FakeResp(
                {"choices": [{"message": {"content": None}}]})):
            try:
                llm.chat_json("sys", "user")
                check("chat-json-null-raises", False, "no exception raised")
            except ValueError:
                check("chat-json-null-raises", True)
            except Exception as exc:  # noqa: BLE001 - any other type fails
                check("chat-json-null-raises", False,
                      f"wrong exception: {type(exc).__name__}")
    finally:
        if old_key is None:
            os.environ.pop("META_API_KEY", None)
        else:
            os.environ["META_API_KEY"] = old_key

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
