-- Actions migration (ADDITIVE ONLY — never edit schema.sql / schema_engine.sql / schema_media.sql).
-- Audit rows for router-decided email actions (app/actions.py).
-- Own code. All statements are idempotent (IF NOT EXISTS) so the
-- migration is safe to re-run. If this table does not exist on a
-- machine yet, actions still run and audit to the structured log
-- line; applying this file turns the log audit into rows as well.
CREATE TABLE IF NOT EXISTS email_actions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  TEXT NOT NULL,
    sender      TEXT NOT NULL DEFAULT '',
    sender_role TEXT NOT NULL DEFAULT 'employee',
    target_role TEXT NOT NULL,
    topic       TEXT NOT NULL DEFAULT '',
    recipient   TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,
    error       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_email_actions_company_created
    ON email_actions (company_id, created_at);
