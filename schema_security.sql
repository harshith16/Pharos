-- Security migration (ADDITIVE ONLY — never edit schema.sql /
-- schema_engine.sql / schema_media.sql / schema_actions.sql).
--
-- 1. Postgres Row Level Security on the tables holding facts.
-- 2. Tables for the 8-digit code gate (hash only, never plaintext).
--
-- How RLS works here: every engine query transaction sets two
-- per-request session variables with SET LOCAL (see app.db
-- scoped_conn): app.current_role (ceo/cfo/cto/employee/manager, or
-- unset) and app.sensitive_unlocked ('on' only while a sender's
-- code unlock window is open, else unset). The SELECT policies
-- below mirror the app role predicates exactly (founder spec,
-- 29 Oct 2026: CEO sees everything; CFO finance + general; CTO
-- technical + general; employee general only; unknown roles get
-- the employee predicate), AND hide every sensitive row unless
-- the unlock variable is 'on'.
--
-- A row is sensitive when its stored visible_to lacks 'employee'
-- (finance rows carry cfo+ceo, technical rows cto+ceo, general
-- rows carry employee). Sensitivity is therefore data-driven, not
-- a word list: it follows the fact's own scope. The
-- sync_is_sensitive trigger recomputes is_sensitive from
-- visible_to on every insert and every visible_to update, so a
-- direct SQL visibility change can never leave a stale flag.
--
-- FORCE ROW LEVEL SECURITY applies the policies even to the table
-- owner, so no query path can bypass them by accident — but it does
-- NOT apply to a superuser or a BYPASSRLS role, which always bypass
-- RLS. The app role must therefore be a plain login role (no
-- superuser, no BYPASSRLS); verify with:
--   SELECT rolname, rolsuper, rolbypassrls FROM pg_roles
--   WHERE rolname = '<app role>';  -- both must be f
-- (pgvector is a trusted extension, so a non-superuser DB owner can
-- still CREATE EXTENSION vector.)
-- Write policies are permissive (writes are app-mediated; RLS here
-- gates reads) — with one Postgres rule every writer must know:
-- a written row version is checked against the SELECT policy, so
-- INSERT ... RETURNING of a restricted-scope row, and any UPDATE
-- that narrows a row into restricted scope (including superseding
-- a restricted fact), can only run on a connection whose variables
-- pass that policy. General rows write anywhere; intake writes
-- (WhatsApp media, /upload) therefore run operator-scoped
-- (ceo + unlocked) — see app.db.scoped_conn. Sender reads stay
-- sender-scoped. Auth tables below deliberately have NO RLS: they
-- hold hashes and gate state, not facts, and are read server-side
-- only.
--
-- All statements are idempotent (IF NOT EXISTS / OR REPLACE /
-- DROP POLICY IF EXISTS) so the migration is safe to re-run.

-- --- Sensitive flag on the fact tables ---------------------------
ALTER TABLE memories ADD COLUMN IF NOT EXISTS is_sensitive BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE engine_relations ADD COLUMN IF NOT EXISTS is_sensitive BOOLEAN NOT NULL DEFAULT FALSE;

-- Backfill + self-correct on every run: sensitivity follows the
-- stored scope, so widening a row to general un-marks it.
UPDATE memories SET is_sensitive = NOT (visible_to @> ARRAY['employee']);
UPDATE engine_relations SET is_sensitive = NOT (visible_to @> ARRAY['employee']);

CREATE OR REPLACE FUNCTION sync_is_sensitive() RETURNS trigger AS $$
BEGIN
    NEW.is_sensitive := NOT (NEW.visible_to @> ARRAY['employee']);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_memories_sensitive ON memories;
CREATE TRIGGER trg_memories_sensitive
    BEFORE INSERT OR UPDATE OF visible_to ON memories
    FOR EACH ROW EXECUTE FUNCTION sync_is_sensitive();

DROP TRIGGER IF EXISTS trg_engine_relations_sensitive ON engine_relations;
CREATE TRIGGER trg_engine_relations_sensitive
    BEFORE INSERT OR UPDATE OF visible_to ON engine_relations
    FOR EACH ROW EXECUTE FUNCTION sync_is_sensitive();

-- --- Code-gate tables (hash only) --------------------------------
-- auth_codes: one row per setup run; verification always uses the
-- most recent row, so re-running the setup script rotates the code.
-- salt + PBKDF2-HMAC-SHA256 hash only. Plaintext is never stored.
CREATE TABLE IF NOT EXISTS auth_codes (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    salt       TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- code_unlocks: one row per sender while their unlock window runs.
CREATE TABLE IF NOT EXISTS code_unlocks (
    sender         TEXT PRIMARY KEY,
    unlocked_until TIMESTAMPTZ NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- code_attempts: consecutive failures + lockout per sender.
CREATE TABLE IF NOT EXISTS code_attempts (
    sender       TEXT PRIMARY KEY,
    failures     INT NOT NULL DEFAULT 0,
    locked_until TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- pending_questions: the question held while a sender is asked for
-- the code. One pending question per sender; a new challenge
-- replaces the previous one.
CREATE TABLE IF NOT EXISTS pending_questions (
    sender     TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    question   TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_codes_created ON auth_codes (created_at DESC);

-- --- RLS policies -------------------------------------------------
-- Role predicate over a table's own visible_to, driven by the
-- per-request variable. Unset/unknown -> employee (least privilege).
-- This fragment is repeated per table because a policy's USING
-- expression must reference that table's own columns.

ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS memories_select_role_sensitive ON memories;
CREATE POLICY memories_select_role_sensitive ON memories FOR SELECT USING (
    (
        CASE current_setting('app.current_role', true)
            WHEN 'ceo' THEN TRUE
            WHEN 'cfo' THEN (visible_to @> ARRAY['cfo'] OR visible_to @> ARRAY['employee'])
            WHEN 'cto' THEN (visible_to @> ARRAY['cto'] OR visible_to @> ARRAY['employee'])
            WHEN 'manager' THEN (visible_to @> ARRAY['manager'] OR visible_to @> ARRAY['employee'])
            ELSE (visible_to @> ARRAY['employee'])
        END
    )
    AND (NOT is_sensitive OR current_setting('app.sensitive_unlocked', true) = 'on')
);
DROP POLICY IF EXISTS memories_write ON memories;
CREATE POLICY memories_write ON memories FOR INSERT WITH CHECK (true);
DROP POLICY IF EXISTS memories_update ON memories;
CREATE POLICY memories_update ON memories FOR UPDATE USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS memories_delete ON memories;
CREATE POLICY memories_delete ON memories FOR DELETE USING (true);

ALTER TABLE engine_relations ENABLE ROW LEVEL SECURITY;
ALTER TABLE engine_relations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS engine_relations_select_role_sensitive ON engine_relations;
CREATE POLICY engine_relations_select_role_sensitive ON engine_relations FOR SELECT USING (
    (
        CASE current_setting('app.current_role', true)
            WHEN 'ceo' THEN TRUE
            WHEN 'cfo' THEN (visible_to @> ARRAY['cfo'] OR visible_to @> ARRAY['employee'])
            WHEN 'cto' THEN (visible_to @> ARRAY['cto'] OR visible_to @> ARRAY['employee'])
            WHEN 'manager' THEN (visible_to @> ARRAY['manager'] OR visible_to @> ARRAY['employee'])
            ELSE (visible_to @> ARRAY['employee'])
        END
    )
    AND (NOT is_sensitive OR current_setting('app.sensitive_unlocked', true) = 'on')
);
DROP POLICY IF EXISTS engine_relations_write ON engine_relations;
CREATE POLICY engine_relations_write ON engine_relations FOR INSERT WITH CHECK (true);
DROP POLICY IF EXISTS engine_relations_update ON engine_relations;
CREATE POLICY engine_relations_update ON engine_relations FOR UPDATE USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS engine_relations_delete ON engine_relations;
CREATE POLICY engine_relations_delete ON engine_relations FOR DELETE USING (true);

-- Edges carry no scope of their own: an edge is visible exactly
-- when its parent fact is visible under the same role + unlock
-- rules. The inner memories read is itself RLS-filtered with the
-- same session variables, so this single EXISTS is the whole gate
-- (a hidden parent hides its edges; a stale orphan with no parent
-- row stays hidden too, which is the safe default — the FK is
-- ON DELETE CASCADE, so orphans should not exist).
ALTER TABLE edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE edges FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS edges_select_via_parent ON edges;
CREATE POLICY edges_select_via_parent ON edges FOR SELECT USING (
    EXISTS (SELECT 1 FROM memories m WHERE m.id = edges.memory_id)
);
DROP POLICY IF EXISTS edges_write ON edges;
CREATE POLICY edges_write ON edges FOR INSERT WITH CHECK (true);
DROP POLICY IF EXISTS edges_update ON edges;
CREATE POLICY edges_update ON edges FOR UPDATE USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS edges_delete ON edges;
CREATE POLICY edges_delete ON edges FOR DELETE USING (true);

-- Engine entities carry no fact content — only names and aliases,
-- which the router must read to link entities before any role
-- decision exists. They stay readable on every connection; the
-- facts themselves are gated in engine_relations above.
ALTER TABLE engine_entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE engine_entities FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS engine_entities_select ON engine_entities;
CREATE POLICY engine_entities_select ON engine_entities FOR SELECT USING (true);
DROP POLICY IF EXISTS engine_entities_write ON engine_entities;
CREATE POLICY engine_entities_write ON engine_entities FOR INSERT WITH CHECK (true);
DROP POLICY IF EXISTS engine_entities_update ON engine_entities;
CREATE POLICY engine_entities_update ON engine_entities FOR UPDATE USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS engine_entities_delete ON engine_entities;
CREATE POLICY engine_entities_delete ON engine_entities FOR DELETE USING (true);
