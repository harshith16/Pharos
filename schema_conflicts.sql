-- Conflict-alert migration (ADDITIVE ONLY — never edit schema.sql /
-- schema_engine.sql / schema_media.sql / schema_actions.sql /
-- schema_security.sql).
--
-- Pending upload conflicts: when a new upload contradicts a stored
-- fact, the conflicting fact is HELD here (never stored) until the
-- uploader replies YES/NO on WhatsApp. On YES the held fact is
-- stored superseding the old one; on NO it is discarded and nothing
-- was ever stored. A second conflict arriving while one is pending
-- for the same sender+company waits as 'queued' and is promoted to
-- 'pending' (and messaged) when the first resolves.
--
-- No RLS on this table: it holds gate state (like the code-gate
-- tables), not facts, and is read server-side only. All statements
-- are idempotent (IF NOT EXISTS) so the migration is safe to re-run.
CREATE TABLE IF NOT EXISTS pending_conflicts (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id       TEXT NOT NULL,
    sender           TEXT NOT NULL,
    old_memory_id    UUID REFERENCES memories(id) ON DELETE CASCADE,
    new_fact         TEXT NOT NULL,
    new_triples      JSONB NOT NULL DEFAULT '[]'::jsonb,
    new_visible_to   TEXT[] NOT NULL DEFAULT ARRAY['employee'],
    new_effective_date DATE,
    new_source       TEXT NOT NULL DEFAULT 'document',
    new_document_id  UUID REFERENCES documents(id) ON DELETE SET NULL,
    new_doc_text     TEXT NOT NULL DEFAULT '',
    new_filename     TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'queued',
                                       'resolved_yes', 'resolved_no',
                                       'stale')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pending_conflicts_sender
    ON pending_conflicts (company_id, sender, status, created_at);
