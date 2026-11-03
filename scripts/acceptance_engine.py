"""Engine acceptance: real Postgres, real code paths, no placeholders.

Proves, against a live database (no LLM key needed — the deterministic
engine paths are exercised; LLM synthesis is used only when a key is
present):
  (a) a 4-hop chain question answered from the full path;
  (b) the same file uploaded twice produces no duplicate facts/relations/
      chunks/documents;
  (c) a second upload adds relations to existing entities (no duplicate
      entity rows);
  (d) chunked ingest of a large generated file (streaming windows,
      DB round-trip reconstruction);
  (e) role filtering on engine relations (employee vs CFO);
  (f) legacy-edges sync into the engine graph;
  (g) engine-to-legacy fallback when the engine finds nothing;
  (h) the live graph page/data endpoints serve only real rows, with an
      honest empty state for an unknown company.

Run: PYTHONPATH=. python scripts/acceptance_engine.py
Exit code 0 iff every check passes. Cleans its own company rows first
(company 'engine-acceptance-co') so reruns are deterministic. Test data
is deliberately left in place afterwards so the /graph page can show it:
open /graph/data?company=engine-acceptance-co
"""

import inspect
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_conn, scoped_conn
from app.engine import dedup, entities, ingest, query, relations
from app.store import write_fact

COMPANY = "engine-acceptance-co"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))


def apply_migrations(conn):
    root = Path(__file__).resolve().parent.parent
    for fname in ("schema.sql", "schema_media.sql", "schema_engine.sql"):
        sql = (root / fname).read_text(encoding="utf-8")
        conn.execute(sql)
    conn.commit()


def cleanup(conn):
    conn.execute("DELETE FROM engine_relations WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM engine_entities WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM engine_uploads WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM memory_sources WHERE memory_id IN "
                 "(SELECT id FROM memories WHERE company_id = %s)", (COMPANY,))
    conn.execute("DELETE FROM edges WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM chunks WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM media WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM documents WHERE company_id = %s", (COMPANY,))
    conn.execute("DELETE FROM memories WHERE company_id = %s", (COMPANY,))
    conn.commit()


def count(conn, table, where=""):
    row = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE company_id = %s {where}", (COMPANY,)
    ).fetchone()
    return row[0]


def entity_id(conn, canonical):
    row = conn.execute(
        "SELECT id FROM engine_entities WHERE company_id = %s AND name = %s",
        (COMPANY, canonical),
    ).fetchone()
    return str(row[0]) if row else None


def relation_exists(conn, src_canonical, rel, dst_canonical):
    src, dst = entity_id(conn, src_canonical), entity_id(conn, dst_canonical)
    if not src or not dst:
        return False
    row = conn.execute(
        "SELECT COUNT(*) FROM engine_relations WHERE company_id = %s "
        "AND src_id = %s AND dst_id = %s AND rel = %s",
        (COMPANY, src, dst, rel),
    ).fetchone()
    return row[0] > 0


def relations_touching(conn, canonical):
    eid = entity_id(conn, canonical)
    if not eid:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) FROM engine_relations WHERE company_id = %s "
        "AND (src_id = %s OR dst_id = %s)",
        (COMPANY, eid, eid),
    ).fetchone()
    return row[0]


def path_steps(res):
    if isinstance(res, dict) and isinstance(res.get("path"), list):
        return res["path"]
    if isinstance(res, list):
        return res
    return []


def step_names_and_rels(steps):
    names, rels = set(), set()
    for s in steps:
        if not isinstance(s, dict):
            continue
        for key in ("from", "to"):
            if s.get(key):
                names.add(s[key])
        if s.get("relationship"):
            rels.add(s["relationship"])
    return names, rels


def main():
    # --- migrations + cleanup -------------------------------------------------
    with get_conn() as conn:
        try:
            apply_migrations(conn)
            check("migrations apply (schema + media + engine)", True)
        except Exception as exc:  # noqa: BLE001
            check("migrations apply (schema + media + engine)", False, str(exc)[:200])
            return 1
        cleanup(conn)

    # --- chunk_stream unit checks (no DB) ------------------------------------
    small = b"abcdefghij" * 1000  # 10,000 bytes
    windows = list(ingest.chunk_stream(io.BytesIO(small), chunk_chars=4000, overlap=200))
    check("chunk_stream is a generator",
          inspect.isgenerator(ingest.chunk_stream(io.BytesIO(b"x"))))
    check("chunk_stream windows/offsets",
          [w[0] for w in windows] == [0, 3800, 7600]
          and windows[0][1] == small[:4000].decode()
          and windows[1][1] == small[3800:7800].decode()
          and windows[2][1] == small[7600:].decode(),
          f"windows={len(windows)}")

    # --- (a) 4-hop chain -------------------------------------------------------
    fixture = (
        "Priya Sharma manages Rahul Verma. "
        "Rahul Verma approves Travel Policy. "
        "Travel Policy sets Transport Budget. "
        "Transport Budget covers Delhi Office."
    )
    with get_conn() as conn:
        res = ingest.ingest_text(conn, COMPANY, fixture, "four-hop.txt")
        conn.commit()
        check("4-hop ingest not duplicate", res.get("duplicate") is False, str(res))
        check("4-hop ingest entities>=5 relations>=4",
              res.get("entities", 0) >= 5 and res.get("relations", 0) >= 4, str(res))
        names_present = all(
            entity_id(conn, n) for n in
            ("priya sharma", "rahul verma", "travel policy",
             "transport budget", "delhi office")
        )
        check("4-hop entities present", bool(names_present))
        rels_present = all((
            relation_exists(conn, "priya sharma", "manages", "rahul verma"),
            relation_exists(conn, "rahul verma", "approves", "travel policy"),
            relation_exists(conn, "travel policy", "sets", "transport budget"),
            relation_exists(conn, "transport budget", "covers", "delhi office"),
        ))
        check("4-hop relations present", rels_present)

        found = relations.find_path(conn, COMPANY, "Delhi Office", "Priya Sharma",
                                    max_hops=4)
        steps = path_steps(found)
        names, rels = step_names_and_rels(steps)
        check("4-hop find_path length 4", len(steps) == 4, f"steps={len(steps)}")
        check("4-hop find_path covers chain",
              names >= {"Priya Sharma", "Rahul Verma", "Travel Policy",
                        "Transport Budget", "Delhi Office"}
              and rels >= {"manages", "approves", "sets", "covers"},
              f"names={sorted(names)} rels={sorted(rels)}")
        trav = relations.traverse(conn, COMPANY, "Delhi Office", max_hops=4)
        check("4-hop traverse contains a 4-step path",
              any(len(path_steps(p)) == 4 for p in trav),
              f"paths={len(trav)}")

        ans = query.answer_with_graph(conn, COMPANY, "employee",
                                      "How is Priya Sharma connected to Delhi Office?")
        check("4-hop engine answer non-empty", ans.get("empty") is False
              and bool(ans.get("answer")), str(ans.get("answer"))[:120])
        check("4-hop engine answer names full chain",
              all(n in (ans.get("answer") or "") for n in
                  ("Priya Sharma", "Rahul Verma", "Travel Policy",
                   "Transport Budget", "Delhi Office")),
              str(ans.get("answer"))[:200])

    # --- (b) same file twice: no duplicates ------------------------------------
    leave_text = ("Annual leave is 30 days per year. "
                  "Unused leave carries forward up to 10 days.\n")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                      encoding="utf-8")
    try:
        tmp.write(leave_text)
        tmp.close()
        with get_conn() as conn:
            first = ingest.ingest_file(conn, COMPANY, tmp.name, "leave.txt")
            conn.commit()
            before = {
                "documents": count(conn, "documents"),
                "chunks": count(conn, "chunks"),
                "entities": count(conn, "engine_entities"),
                "relations": count(conn, "engine_relations"),
            }
            second = ingest.ingest_file(conn, COMPANY, tmp.name, "leave.txt")
            conn.commit()
            after = {
                "documents": count(conn, "documents"),
                "chunks": count(conn, "chunks"),
                "entities": count(conn, "engine_entities"),
                "relations": count(conn, "engine_relations"),
            }
        check("re-upload flagged duplicate", second.get("duplicate") is True,
              str(second))
        check("re-upload adds no rows", before == after,
              f"before={before} after={after}")
        check("re-upload document row unique",
              first.get("document_id") is not None
              and first.get("content_hash") == second.get("content_hash"))
    finally:
        os.unlink(tmp.name)

    # --- direct dedup helpers ---------------------------------------------------
    with get_conn() as conn:
        h = dedup.content_hash_text("direct-dedup-probe")
        is_new, uid = dedup.register_upload(conn, COMPANY, h, "probe.txt")
        is_new2, uid2 = dedup.register_upload(conn, COMPANY, h, "probe.txt")
        conn.commit()
        check("dedup register then conflict",
              is_new is True and is_new2 is False and uid == uid2)
        check("dedup is_duplicate_file", dedup.is_duplicate_file(conn, COMPANY, h))

    # --- (c) second upload adds relations to existing entities ------------------
    with get_conn() as conn:
        before_touch = relations_touching(conn, "rahul verma")
        res = ingest.ingest_text(conn, COMPANY, "Rahul Verma leads Delhi Office.",
                                 "leads.txt")
        conn.commit()
        row = conn.execute(
            "SELECT COUNT(*) FROM engine_entities WHERE company_id = %s "
            "AND name = %s", (COMPANY, "rahul verma")).fetchone()
        check("incremental upload keeps one Rahul entity", row[0] == 1,
              f"count={row[0]}")
        check("incremental upload adds a Rahul relation",
              relations_touching(conn, "rahul verma") == before_touch + 1,
              f"before={before_touch} after={relations_touching(conn, 'rahul verma')}")
        check("incremental new relation present",
              relation_exists(conn, "rahul verma", "leads", "delhi office"))

    # --- merge helpers ------------------------------------------------------------
    with get_conn() as conn:
        keep = entities.resolve_entity(conn, COMPANY, "Alpha Team")
        drop = entities.resolve_entity(conn, COMPANY, "Alpha Team North")
        conn.execute(
            "UPDATE engine_entities SET aliases = %s::jsonb WHERE id = %s",
            ('["alpha team"]', drop))
        conn.commit()
        merged = dedup.merge_alias_collisions(conn, COMPANY)
        conn.commit()
        row = conn.execute(
            "SELECT COUNT(*) FROM engine_entities WHERE company_id = %s "
            "AND name = %s", (COMPANY, "alpha team north")).fetchone()
        check("alias collision merges away duplicate", merged >= 1 and row[0] == 0,
              f"merged={merged}")
        e1 = entities.resolve_entity(conn, COMPANY, "Beta Team")
        e2 = entities.resolve_entity(conn, COMPANY, "Beta Team Old")
        relations.add_relation(conn, COMPANY, "Beta Team Old", "manages",
                               "Delhi Office")
        conn.commit()
        entities.merge_entities(conn, COMPANY, e1, e2)
        conn.commit()
        drop_row = conn.execute(
            "SELECT COUNT(*) FROM engine_entities WHERE company_id = %s "
            "AND name = %s", (COMPANY, "beta team old")).fetchone()
        keep_aliases = conn.execute(
            "SELECT aliases FROM engine_entities WHERE company_id = %s "
            "AND name = %s", (COMPANY, "beta team")).fetchone()
        check("merge_entities repoints and drops",
              drop_row[0] == 0
              and "beta team old" in (keep_aliases[0] or [])
              and relation_exists(conn, "beta team", "manages", "delhi office"),
              f"drop_rows={drop_row[0]} aliases={keep_aliases[0]}")

    # --- (e) role filtering on engine relations -----------------------------------
    with get_conn() as conn:
        relations.add_relation(conn, COMPANY, "Finance Board", "approves",
                               "Budget Office")
        conn.commit()
        row = conn.execute(
            "SELECT id FROM engine_relations WHERE company_id = %s "
            "AND rel = %s ORDER BY created_at DESC LIMIT 1",
            (COMPANY, "approves")).fetchone()
        # Restrict the newest approves relation (Finance Board -> Budget Office)
        # to finance+CEO only; find it precisely via its endpoints.
        # The narrowing UPDATE needs operator scope: Postgres checks
        # a written row version against the SELECT policy, so a
        # restricted new version fails on a default connection.
        src, dst = entity_id(conn, "finance board"), entity_id(conn, "budget office")
        with scoped_conn("ceo", unlocked=True) as uconn:
            uconn.execute(
                "UPDATE engine_relations SET visible_to = ARRAY['cfo','ceo'] "
                "WHERE company_id = %s AND src_id = %s AND dst_id = %s AND rel = %s",
                (COMPANY, src, dst, "approves"))
        conn.commit()
        emp = query.answer_with_graph(conn, COMPANY, "employee",
                                      "How is Finance Board connected to Budget Office?")
        # RLS: the restricted relation is sensitive (visible_to lacks
        # 'employee'), so the CFO graph read needs a scoped connection
        # with the unlock on — the same state a correct code creates.
        # The employee read above stays on the default connection.
        with scoped_conn("cfo", unlocked=True) as cfo_conn:
            cfo = query.answer_with_graph(
                cfo_conn, COMPANY, "cfo",
                "How is Finance Board connected to Budget Office?")
        check("role filter hides finance path from employee",
              emp.get("empty") is True, str(emp.get("answer"))[:80])
        check("role filter shows finance path to CFO",
              cfo.get("empty") is False
              and "Finance Board" in (cfo.get("answer") or "")
              and "Budget Office" in (cfo.get("answer") or ""),
              str(cfo.get("answer"))[:160])

    # --- (f) legacy edges sync ------------------------------------------------------
    with get_conn() as conn:
        write_fact(conn, COMPANY, "Leave policy limit is 30 days.",
                   source="document",
                   triples=[{"source": "leave_policy", "relationship": "limit",
                             "destination": "30_days"}])
        conn.commit()
        synced = relations.sync_from_edges(conn, COMPANY)
        conn.commit()
        check("sync_from_edges materializes legacy triples",
              synced.get("relations_created", 0) >= 1,
              str(synced))
        check("sync_from_edges entity present",
              entity_id(conn, "leave_policy") is not None)

    # --- (g) engine-to-legacy fallback ------------------------------------------------
    with get_conn() as conn:
        fb = query.answer_question_engine_or_legacy(
            conn, COMPANY, "employee", "zzzqqq unknown thing")
        check("legacy fallback flag on empty engine result",
              fb.get("engine_fallback") is True and "answer" in fb)

    # --- (d) large chunked file -------------------------------------------------------
    sentence = ("quarterly review notes cover regional budgets and staffing "
                "plans for the Delhi Office. ")
    target_chars = 3_000_000
    big = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                      encoding="utf-8")
    try:
        written = 0
        block = sentence * 750
        while written < target_chars:
            big.write(block)
            written += len(block)
        big.close()
        with open(big.name, "r", encoding="utf-8") as fh:
            original = fh.read()
        with get_conn() as conn:
            res = ingest.ingest_file(conn, COMPANY, big.name, "big-review.txt")
            conn.commit()
            rows = conn.execute(
                "SELECT content FROM chunks WHERE company_id = %s AND "
                "document_id = %s ORDER BY position",
                (COMPANY, res["document_id"])).fetchall()
        rebuilt = rows[0][0] + "".join(r[0][200:] for r in rows[1:])
        check("large ingest chunked (>100 chunks)",
              res.get("chunks", 0) > 100, f"chunks={res.get('chunks')}")
        check("large ingest round-trips exactly", rebuilt == original,
              f"orig={len(original)} rebuilt={len(rebuilt)}")
        check("large ingest not duplicate", res.get("duplicate") is False)
    finally:
        os.unlink(big.name)

    # --- (h) graph endpoints serve only real rows --------------------------------------
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app
    client = TestClient(fastapi_app)
    r = client.get("/graph/data", params={"company": COMPANY})
    data = r.json()
    node_labels = {n.get("label") for n in data.get("nodes", [])}
    edge_labels = {e.get("label") for e in data.get("edges", [])}
    check("graph data 200 with real nodes",
          r.status_code == 200 and "Priya Sharma" in node_labels,
          f"nodes={len(data.get('nodes', []))}")
    check("graph data real edges", "manages" in edge_labels,
          f"edges={len(data.get('edges', []))}")
    r2 = client.get("/graph/data", params={"company": "no-such-company-xyz"})
    d2 = r2.json()
    check("graph data honest empty state",
          r2.status_code == 200 and d2.get("nodes") == [] and d2.get("edges") == [])
    r3 = client.get("/graph")
    check("graph page 200 with data hook",
          r3.status_code == 200 and "/graph/data" in r3.text)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} engine checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
