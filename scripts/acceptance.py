"""Acceptance test: real DB, real code paths, no placeholders.

Seed travel limit 8000, supersede to 12000 from a chat message, then
prove: (a) current returns 12000 only; (b) history returns both with
the chat message as evidence; (c) a superseded-only query produces the
conflict warning; (d) hierarchy roles gate finance/technical facts
(CEO sees all, CFO finance not technical, CTO technical not finance,
employee general only).
Plus: keyword path with vectors disabled, vector assertions when Ollama
is up, as-of history, media ingest, explain fields, dedup, the
supersede transaction, HTTP surfaces, WhatsApp role mapping, WhatsApp
media parsing + webhook intake, and document text extraction.

Run: PYTHONPATH=. python scripts/acceptance.py
Exit code 0 iff every check passes. Cleans its own company rows first
so reruns are deterministic.
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import embed
from app.answer import answer_question
from app.db import get_conn
from app.extract_text import extract_text
from app.retrieve import get_timeline, search, search_as_of
from app.store import ingest_chat_message, ingest_media, link_media_to_fact, write_fact

COMPANY = "acceptance-co"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))


def cleanup(conn):
    conn.execute("DELETE FROM edges WHERE company_id = %s", (COMPANY,))
    conn.execute(
        "DELETE FROM memory_sources WHERE memory_id IN "
        "(SELECT id FROM memories WHERE company_id = %s)",
        (COMPANY,),
    )
    conn.execute("DELETE FROM media WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM chunks WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM documents WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM memories WHERE company_id = %s", (COMPANY,))
    # Engine tables are new since this suite was written; wipe them
    # too so reruns stay deterministic (engine ingest dedups on text
    # hash, so a stale upload row would otherwise suppress re-ingest).
    try:
        conn.execute(
            "DELETE FROM engine_relations WHERE company_id = %s", (COMPANY,)
        )
        conn.execute(
            "DELETE FROM engine_entities WHERE company_id = %s", (COMPANY,)
        )
        conn.execute(
            "DELETE FROM engine_uploads WHERE company_id = %s", (COMPANY,)
        )
    except Exception:
        pass
    conn.commit()


def _minimal_pdf(text):
    """Build a tiny one-page PDF with a text layer (test fixture)."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
        + content
        + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


def main():
    vectors_up = embed.vectors_available()
    print(f"vectors_available at start: {vectors_up}")
    with get_conn() as conn:
        cleanup(conn)

        # --- Seed: 8000, then supersede to 12000 from a chat message ---
        old_id = write_fact(
            conn,
            COMPANY,
            "Travel limit is 8000 rupees per trip.",
            domain="general",
            effective_date=date(2026, 1, 1),
            source="manual",
            triples=[
                {
                    "source": "travel_policy",
                    "relationship": "limit",
                    "destination": "8000",
                }
            ],
        )
        ingest_chat_message(
            conn,
            COMPANY,
            "msg-travel-12000",
            "Hi team, travel limit is now 12000 rupees per trip, "
            "effective October 1. - Rahul",
        )
        new_id = write_fact(
            conn,
            COMPANY,
            "Travel limit is 12000 rupees per trip.",
            domain="general",
            effective_date=date(2026, 10, 1),
            source="chat",
            source_message_id="msg-travel-12000",
            triples=[
                {
                    "source": "travel_policy",
                    "relationship": "limit",
                    "destination": "12000",
                }
            ],
            supersedes_id=old_id,
            relation="updates",
        )
        fin_id = write_fact(
            conn,
            COMPANY,
            "Quarterly finance close is approved by the CFO.",
            domain="finance",
            effective_date=date(2026, 1, 1),
            source="manual",
            triples=[
                {
                    "source": "finance_policy",
                    "relationship": "close_approver",
                    "destination": "cfo",
                }
            ],
        )
        tech_id = write_fact(
            conn,
            COMPANY,
            "Production deploys need CTO approval.",
            domain="technical",
            effective_date=date(2026, 1, 1),
            source="manual",
            triples=[
                {
                    "source": "deploy_policy",
                    "relationship": "approver",
                    "destination": "cto",
                }
            ],
        )

        # (a) Current returns 12000 only.
        ans = answer_question(conn, COMPANY, "employee", "What is the travel limit?")
        current_facts = [f["fact"] for f in ans["facts"]]
        check(
            "a-current-12000-only",
            any("12000" in f for f in current_facts)
            and not any("8000" in f for f in current_facts),
            f"facts={current_facts}",
        )

        # (b) History returns both, chat message as evidence.
        chain = get_timeline(conn, COMPANY, new_id)
        chain_facts = [e["fact"] for e in chain]
        v2 = chain[-1] if chain else {}
        src_row = conn.execute(
            "SELECT source_type, source_ref FROM memory_sources "
            "WHERE memory_id = %s",
            (new_id,),
        ).fetchone()
        check(
            "b-history-both-chat-evidence",
            len(chain) == 2
            and any("8000" in f for f in chain_facts)
            and any("12000" in f for f in chain_facts)
            and chain[0]["version"] == 1
            and chain[1]["version"] == 2
            and v2.get("source_message_id") == "msg-travel-12000"
            and src_row is not None
            and src_row[0] == "chat"
            and src_row[1] == "msg-travel-12000",
            f"chain={chain_facts} src={src_row}",
        )

        # (c) Superseded-only query -> conflict warning.
        cur = search(conn, COMPANY, "employee", "travel limit 8000")
        all_rows = search(
            conn, COMPANY, "employee", "travel limit 8000", include_superseded=True
        )
        cur_ids = {r["id"] for r in cur}
        all_ids = {r["id"] for r in all_rows}
        ans_c = answer_question(conn, COMPANY, "employee", "travel limit 8000")
        check(
            "c-conflict-warning",
            old_id in all_ids
            and old_id not in cur_ids
            and ans_c["conflict"] is not None
            and "8000" in ans_c["conflict"],
            f"conflict={ans_c['conflict']}",
        )

        # (d) Hierarchy roles: CEO sees all, CFO finance not
        # technical, CTO technical not finance, employee general
        # only. Travel (general) is visible to every role.
        def _ids(rows):
            return {r["id"] for r in rows}

        ceo_fin = _ids(search(conn, COMPANY, "ceo", "finance close"))
        cfo_fin = _ids(search(conn, COMPANY, "cfo", "finance close"))
        cto_fin = _ids(search(conn, COMPANY, "cto", "finance close"))
        emp_fin = _ids(search(conn, COMPANY, "employee", "finance close"))
        ceo_tech = _ids(search(conn, COMPANY, "ceo", "production deploys"))
        cfo_tech = _ids(search(conn, COMPANY, "cfo", "production deploys"))
        cto_tech = _ids(search(conn, COMPANY, "cto", "production deploys"))
        emp_tech = _ids(search(conn, COMPANY, "employee", "production deploys"))
        ceo_travel = answer_question(conn, COMPANY, "ceo", "What is the travel limit?")
        emp_travel = answer_question(
            conn, COMPANY, "employee", "What is the travel limit?"
        )
        ceo_travel_ids = [f["id"] for f in ceo_travel["facts"]]
        emp_travel_ids = [f["id"] for f in emp_travel["facts"]]
        check(
            "d-role-scoping",
            fin_id in ceo_fin
            and fin_id in cfo_fin
            and fin_id not in cto_fin
            and fin_id not in emp_fin
            and tech_id in ceo_tech
            and tech_id in cto_tech
            and tech_id not in cfo_tech
            and tech_id not in emp_tech
            and new_id in ceo_travel_ids
            and new_id in emp_travel_ids,
            f"fin ceo/cfo/cto/emp="
            f"{fin_id in ceo_fin}/{fin_id in cfo_fin}/"
            f"{fin_id in cto_fin}/{fin_id in emp_fin} "
            f"tech ceo/cfo/cto/emp="
            f"{tech_id in ceo_tech}/{tech_id in cfo_tech}/"
            f"{tech_id in cto_tech}/{tech_id in emp_tech}",
        )

        # Keyword path with vectors disabled must give the same current answer.
        real_available = embed.vectors_available
        embed.vectors_available = lambda timeout=2.0: False
        try:
            kw = search(conn, COMPANY, "employee", "travel limit")
        finally:
            embed.vectors_available = real_available
        check(
            "keyword-only-path",
            any("12000" in r["fact"] for r in kw)
            and not any("8000" in r["fact"] for r in kw),
            f"kw_facts={[r['fact'] for r in kw]}",
        )

        # Vector assertions, only when Ollama is actually up.
        if vectors_up:
            row = conn.execute(
                "SELECT embedding FROM memories WHERE id = %s", (new_id,)
            ).fetchone()
            raw_vec = row[0] if row else None
            # psycopg returns pgvector as its text form without the
            # pgvector adapter registered; count dims from either form.
            if isinstance(raw_vec, str):
                dims = len(raw_vec.strip().strip("[]").split(","))
            elif raw_vec is not None:
                dims = len(raw_vec)
            else:
                dims = None
            para = search(
                conn, COMPANY, "employee", "maximum amount allowed for a business trip"
            )
            para_top = para[:3]
            check(
                "vector-assertions",
                raw_vec is not None
                and dims == 768
                and any(r["id"] == new_id for r in para_top)
                and any(
                    r["scores"]["score_details"]["semantic_score"] >= 0.1
                    for r in para_top
                    if r["id"] == new_id
                ),
                f"stored_dims={dims} " f"para_top={[r['fact'] for r in para_top]}",
            )
        else:
            print("SKIP: vector-assertions (Ollama not available)")

        # As-of history.
        asof_old = search_as_of(conn, COMPANY, "employee", "travel limit", "2026-06-01")
        asof_new = search_as_of(conn, COMPANY, "employee", "travel limit", "2026-12-01")
        check(
            "as-of-history",
            asof_old
            and "8000" in asof_old[0]["fact"]
            and asof_new
            and "12000" in asof_new[0]["fact"],
            f"jun={[r['fact'] for r in asof_old[:1]]} "
            f"dec={[r['fact'] for r in asof_new[:1]]}",
        )

        # Media ingest: honest embedded flags + graph link.
        mid_cap = ingest_media(
            conn,
            COMPANY,
            "receipt.jpg",
            "image/jpeg",
            "media/x_receipt.jpg",
            caption="Hotel receipt for Rahul",
        )
        row_cap = conn.execute(
            "SELECT embedded, embedding FROM media WHERE id = %s", (mid_cap,)
        ).fetchone()
        mid_plain = ingest_media(
            conn, COMPANY, "photo.png", "image/png", "media/x_photo.png"
        )
        row_plain = conn.execute(
            "SELECT embedded, embedding FROM media WHERE id = %s", (mid_plain,)
        ).fetchone()
        edge_id = link_media_to_fact(conn, COMPANY, mid_cap, new_id)
        edge_row = conn.execute(
            "SELECT subject, relationship, object FROM edges WHERE id = %s",
            (edge_id,),
        ).fetchone()
        other_fact = write_fact(
            conn,
            "other-co",
            "Other company travel limit is 100 rupees.",
            ["employee", "manager"],
            source="manual",
        )
        try:
            link_media_to_fact(conn, COMPANY, mid_cap, other_fact)
            wrong_company_ok = False
        except ValueError:
            wrong_company_ok = True
        conn.execute("DELETE FROM memories WHERE company_id = %s", ("other-co",))
        conn.commit()
        media_ok = (
            row_cap[0] == vectors_up
            and (row_cap[1] is not None) == vectors_up
            and row_plain[0] is False
            and row_plain[1] is None
            and edge_row is not None
            and edge_row[0] == f"media:{mid_cap}"
            and edge_row[1] == "evidence_for"
            and wrong_company_ok
        )
        check(
            "media-ingest-honest-flags",
            media_ok,
            f"caption_embedded={row_cap[0]} plain_embedded={row_plain[0]} "
            f"edge={edge_row} wrong_company_raises={wrong_company_ok}",
        )

        # Explain fields present.
        res = search(conn, COMPANY, "employee", "travel limit")
        sd = res[0]["scores"]["score_details"] if res else {}
        check(
            "explain-fields",
            set(sd.keys())
            == {
                "semantic_score",
                "bm25_score",
                "entity_boost",
                "raw_score",
                "max_possible_score",
                "final_score",
                "threshold",
            },
            f"keys={sorted(sd.keys())}",
        )

        # Dedup: identical fact returns the same id, no new row.
        before = conn.execute(
            "SELECT count(*) FROM memories WHERE company_id = %s", (COMPANY,)
        ).fetchone()[0]
        dup_id = write_fact(
            conn,
            COMPANY,
            "Travel limit is 12000 rupees per trip.",
            domain="general",
            effective_date=date(2026, 10, 1),
            source="chat",
            source_message_id="msg-travel-12000",
        )
        after = conn.execute(
            "SELECT count(*) FROM memories WHERE company_id = %s", (COMPANY,)
        ).fetchone()[0]
        check(
            "dedup-same-id",
            dup_id == new_id and before == after,
            f"dup={dup_id == new_id} count={before}->{after}",
        )

        # Supersede transaction shape: old flipped, new versioned, edge
        # stored on the NEW fact keyed by the OLD fact's id.
        import json as _json

        old_row = conn.execute(
            "SELECT version, is_latest, status FROM memories " "WHERE id = %s",
            (old_id,),
        ).fetchone()
        new_row = conn.execute(
            "SELECT version, parent_memory_id, root_memory_id, "
            "memory_relations FROM memories WHERE id = %s",
            (new_id,),
        ).fetchone()
        rels = new_row[3] if new_row else {}
        if isinstance(rels, str):
            rels = _json.loads(rels)
        check(
            "supersede-transaction",
            old_row[0] == 1
            and old_row[1] is False
            and old_row[2] == "superseded"
            and new_row[0] == 2
            and str(new_row[1]) == old_id
            and str(new_row[2]) == old_id
            and rels.get(old_id) == "updates",
            f"old={old_row} new_version={new_row[0]} rels={rels}",
        )

        # HTTP surfaces: health + timeline page. The timeline handler
        # scopes by COMPANY_ID env, so point it at the test company.
        os.environ["COMPANY_ID"] = COMPANY
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        health = client.get("/health")
        page = client.get(f"/timeline/{new_id}")
        check(
            "http-surfaces",
            health.status_code == 200
            and health.json().get("status") == "ok"
            and page.status_code == 200
            and "Policy timeline" in page.text
            and "12000" in page.text,
            f"health={health.status_code} timeline={page.status_code}",
        )

        # WhatsApp role mapping: CEO/CFO/CTO lists, manager alias,
        # unknown sender -> employee.
        import app.whatsapp as wa_mod

        role_env = {}
        for key in (
            "WHATSAPP_CEO_PHONE",
            "WHATSAPP_CFO_PHONE",
            "WHATSAPP_CTO_PHONE",
            "WHATSAPP_MANAGER_PHONE",
        ):
            role_env[key] = os.environ.get(key)
        os.environ["WHATSAPP_CEO_PHONE"] = "15550002222"
        os.environ["WHATSAPP_CFO_PHONE"] = "15550003333, 15550004444"
        os.environ["WHATSAPP_CTO_PHONE"] = "15550005555"
        os.environ["WHATSAPP_MANAGER_PHONE"] = "15550001111"
        try:
            role_ok = (
                wa_mod._role_for("15550001111") == "ceo"
                and wa_mod._role_for("15550002222") == "ceo"
                and wa_mod._role_for("15550003333") == "cfo"
                and wa_mod._role_for("15550004444") == "cfo"
                and wa_mod._role_for("15550005555") == "cto"
                and wa_mod._role_for("999") == "employee"
            )
        finally:
            for key, val in role_env.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
        check(
            "whatsapp-role-alias",
            role_ok,
            "manager->ceo, lists, unknown->employee",
        )

        # WhatsApp media parsing: text + document + image in, audio out.
        from app.whatsapp import parse_incoming

        wa_payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "111",
                                        "id": "m1",
                                        "timestamp": "1",
                                        "type": "text",
                                        "text": {"body": "hello"},
                                    },
                                    {
                                        "from": "111",
                                        "id": "m2",
                                        "timestamp": "1",
                                        "type": "document",
                                        "document": {
                                            "id": "med1",
                                            "filename": "policy.pdf",
                                            "mime_type": "application/pdf",
                                            "caption": "read this",
                                        },
                                    },
                                    {
                                        "from": "111",
                                        "id": "m3",
                                        "timestamp": "1",
                                        "type": "image",
                                        "image": {
                                            "id": "med2",
                                            "mime_type": "image/jpeg",
                                            "caption": "site photo",
                                        },
                                    },
                                    {
                                        "from": "111",
                                        "id": "m4",
                                        "timestamp": "1",
                                        "type": "audio",
                                        "audio": {"id": "med3"},
                                    },
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        parsed = parse_incoming(wa_payload)
        check(
            "whatsapp-media-parse",
            len(parsed) == 3
            and parsed[0]["kind"] == "text"
            and parsed[0]["text"] == "hello"
            and parsed[1]["kind"] == "document"
            and parsed[1]["media_id"] == "med1"
            and parsed[1]["filename"] == "policy.pdf"
            and parsed[1]["caption"] == "read this"
            and parsed[2]["kind"] == "image"
            and parsed[2]["media_id"] == "med2"
            and parsed[2]["filename"] == "image_m3.jpg",
            f"parsed={[(p['kind'], p.get('filename')) for p in parsed]}",
        )

        # Document text extraction: TXT, DOCX, PDF in; image/empty out.
        import io as _io

        from docx import Document as _Docx

        _buf = _io.BytesIO()
        _d = _Docx()
        _d.add_paragraph("Quarterly budget review notes")
        _d.save(_buf)
        check(
            "doc-extraction",
            (extract_text("notes.txt", b"Travel limit is 9000 rupees.") or "")
            .strip()
            == "Travel limit is 9000 rupees."
            and "Quarterly budget review notes"
            in (extract_text("notes.docx", _buf.getvalue()) or "")
            and "Quarterly budget review"
            in (extract_text("notes.pdf", _minimal_pdf("Quarterly budget review")) or "")
            and extract_text("photo.png", b"\x89PNG\r\n\x1a\n") is None
            and extract_text("empty.txt", b"  \n ") is None,
            "txt/docx/pdf ok, png/empty -> None",
        )

        # WhatsApp media webhook, two honest paths under the new
        # background contract: POST returns 200 + accepted at once
        # and the work happens after. (1) No Graph token: nothing
        # is downloaded, ingested, or sent. (2) Dummy token +
        # patched download/deliver/doc-reply (no real Graph or LLM
        # calls): the full intake pipeline runs — save, ingest
        # (legacy + engine), honest confirmation — and the
        # confirmation body is captured from the send path.
        import tempfile

        wa_payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "15550009999",
                                        "id": "wamid-doc-1",
                                        "timestamp": "1700000000",
                                        "type": "document",
                                        "document": {
                                            "id": "media-txt-1",
                                            "filename": "policy.txt",
                                            "mime_type": "text/plain",
                                            "caption": "new policy",
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        wa_env = {}
        for key in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_ID", "MEDIA_DIR"):
            wa_env[key] = os.environ.get(key)
            os.environ.pop(key, None)
        os.environ["COMPANY_ID"] = COMPANY
        os.environ["MEDIA_DIR"] = tempfile.mkdtemp(prefix="wa_media_")
        real_download = wa_mod._download_media
        real_deliver = wa_mod._deliver
        llm_mod = None
        real_doc_reply = None

        async def _fake_download(token, media_id):
            assert media_id == "media-txt-1"
            return b"Travel limit is 9000 rupees per trip.", "text/plain"

        sent_bodies = []

        async def _fake_deliver(client, phone_id, token, sender, body):
            sent_bodies.append(body)
            return "sent", "fake-msg-id"

        wa_mod._download_media = _fake_download
        try:
            wa_client = TestClient(app)
            # Path 1: no token -> 200 + accepted, and the background
            # task skips everything: nothing ingested, nothing sent.
            import time

            wa_resp = wa_client.post("/webhook/whatsapp", json=wa_payload)
            wa_body = wa_resp.json()
            time.sleep(0.5)
            doc_row1 = conn.execute(
                "SELECT custom_id FROM documents WHERE custom_id = %s",
                ("whatsapp:wamid-doc-1",),
            ).fetchone()
            media_row1 = conn.execute(
                "SELECT filename FROM media WHERE company_id = %s "
                "AND filename = %s",
                (COMPANY, "policy.txt"),
            ).fetchone()
            path1_ok = (
                wa_resp.status_code == 200
                and wa_body.get("status") == "accepted"
                and doc_row1 is None
                and media_row1 is None
                and not sent_bodies
            )
            # Path 2: dummy token + patched Graph calls -> full intake.
            # The doc-reply LLM call is stubbed (no Meta key in this
            # VM); the pipeline — save, ingest, deliver — is real.
            os.environ["WHATSAPP_TOKEN"] = "dummy-token"
            os.environ["WHATSAPP_PHONE_ID"] = "12345"
            wa_mod._deliver = _fake_deliver
            import app.llm as llm_mod

            real_doc_reply = llm_mod.generate_doc_reply
            llm_mod.generate_doc_reply = lambda filename, text, n: (
                f"Got it — saved {filename} and added it to the brain."
            )
            wa_resp2 = wa_client.post("/webhook/whatsapp", json=wa_payload)
            wa_body2 = wa_resp2.json()
            confirm_text = ""
            doc_row = None
            media_row = None
            engine_row = None
            deadline = time.time() + 5.0
            while time.time() < deadline:
                confirm_text = sent_bodies[0] if sent_bodies else ""
                doc_row = conn.execute(
                    "SELECT custom_id FROM documents WHERE custom_id = %s",
                    ("whatsapp:wamid-doc-1",),
                ).fetchone()
                media_row = conn.execute(
                    "SELECT filename FROM media WHERE company_id = %s "
                    "AND filename = %s",
                    (COMPANY, "policy.txt"),
                ).fetchone()
                engine_row = conn.execute(
                    "SELECT custom_id FROM documents WHERE company_id = %s "
                    "AND title = %s AND custom_id LIKE 'engine:%%'",
                    (COMPANY, "policy.txt"),
                ).fetchone()
                if confirm_text and doc_row and media_row and engine_row:
                    break
                time.sleep(0.2)
            path2_ok = (
                wa_resp2.status_code == 200
                and wa_body2.get("status") == "accepted"
                and "policy.txt" in confirm_text
                and ("saved" in confirm_text or "added" in confirm_text)
                and doc_row is not None
                and media_row is not None
                and engine_row is not None
            )
            check(
                "whatsapp-media-webhook",
                path1_ok and path2_ok,
                f"path1={path1_ok} "
                f"path2={path2_ok} confirm={confirm_text[:60]} "
                f"doc={doc_row} media={media_row} engine={engine_row}",
            )
        finally:
            wa_mod._download_media = real_download
            wa_mod._deliver = real_deliver
            if llm_mod is not None:
                llm_mod.generate_doc_reply = real_doc_reply
            for key, val in wa_env.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
            os.environ["COMPANY_ID"] = COMPANY

    passed = sum(1 for _, ok in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} PASS")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
