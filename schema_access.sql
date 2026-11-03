-- Access-request migration (ADDITIVE ONLY — never edit earlier schemas).
-- Pending role-block access requests (app/roleblock.py): one pending
-- request per sender+company; a new role-block replaces the previous
-- one. Gate state, server-side only — no RLS, like the code-gate
-- tables. All statements are idempotent (IF NOT EXISTS) so the
-- migration is safe to re-run.
CREATE TABLE IF NOT EXISTS pending_access_requests (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id   TEXT NOT NULL,
    sender       TEXT NOT NULL,
    scope_label  TEXT NOT NULL,
    approver     TEXT NOT NULL,
    approver_role TEXT NOT NULL,
    question     TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'resolved_sent',
                                   'resolved_no',
                                   'resolved_unconfigured')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pending_access_company_sender
    ON pending_access_requests (company_id, sender, status, created_at);
