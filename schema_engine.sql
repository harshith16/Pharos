-- Engine migration (ADDITIVE ONLY — never edit schema.sql / schema_media.sql).
-- Graph + dedup + chunked-ingest tables for app/engine/.
-- Own code, patterns only. All tables are company-scoped; all statements
-- are idempotent (IF NOT EXISTS) so the migration is safe to re-run.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS engine_entities (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id   TEXT NOT NULL,
    name         TEXT NOT NULL,
    display_name TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'entity',
    aliases      JSONB NOT NULL DEFAULT '[]'::jsonb,
    embedding    vector(768),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, name)
);

CREATE TABLE IF NOT EXISTS engine_relations (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id TEXT NOT NULL,
    src_id     UUID NOT NULL REFERENCES engine_entities(id) ON DELETE CASCADE,
    dst_id     UUID NOT NULL REFERENCES engine_entities(id) ON DELETE CASCADE,
    rel        TEXT NOT NULL,
    evidence   JSONB NOT NULL DEFAULT '[]'::jsonb,
    visible_to TEXT[] NOT NULL DEFAULT ARRAY['employee'],
    weight     FLOAT NOT NULL DEFAULT 1.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, src_id, dst_id, rel)
);

CREATE TABLE IF NOT EXISTS engine_uploads (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id   TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    filename     TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_engine_entities_company_name
    ON engine_entities (company_id, name);
CREATE INDEX IF NOT EXISTS idx_engine_entities_embedding
    ON engine_entities USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_engine_relations_src
    ON engine_relations (company_id, src_id);
CREATE INDEX IF NOT EXISTS idx_engine_relations_dst
    ON engine_relations (company_id, dst_id);
CREATE INDEX IF NOT EXISTS idx_engine_relations_visible
    ON engine_relations USING GIN (visible_to);
CREATE INDEX IF NOT EXISTS idx_engine_uploads_company_hash
    ON engine_uploads (company_id, content_hash);
