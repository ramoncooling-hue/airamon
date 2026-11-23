-- @idempotent-alter
-- T1.4 / O-REL-2: explicit approval lease.
--
-- An approval is claimed with a short lease in the same write transaction that
-- moves proposed -> approved. A retried or concurrent approve sees a live lease
-- and returns the current row instead of running the local write twice; a
-- crashed executor leaves an expired lease that recover_stale() turns into a
-- retryable failure. migrations.py skips an ALTER whose column already exists
-- (SQLite has no ADD COLUMN IF NOT EXISTS).
ALTER TABLE agent_actions ADD COLUMN approved_at TEXT;
ALTER TABLE agent_actions ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_actions ADD COLUMN lease_expires_at TEXT;
CREATE INDEX IF NOT EXISTS idx_agent_actions_lease
    ON agent_actions(status, lease_expires_at);
