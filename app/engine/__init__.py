"""Production graph engine (additive, on top of app.store/app.retrieve).

Modules:
- entities: canonical entity extraction + resolution + merging.
- relations: typed relations, multi-hop traversal (up to 4 hops), sync from
  the existing edges table.
- dedup: content-hash upload dedup + alias-collision merging.
- ingest: chunked, streaming intake that never loads a whole file into
  memory; incremental (new uploads attach to existing entities).
- query: multi-hop question answering with evidence paths, respecting the
  existing role filter, falling back to app.answer on any failure.

This package never rewrites existing modules. Importing this package has
no side effects: submodules are imported explicitly by callers.
"""
