-- Expand the action CHECK without changing recorded historical migrations.
-- Keep the AUTOINCREMENT high-water mark even when its last row was deleted.
CREATE TEMP TABLE agent_actions_030_sequence (seq INTEGER NOT NULL);
INSERT INTO agent_actions_030_sequence(seq)
    SELECT COALESCE(MAX(seq), 0) FROM sqlite_sequence WHERE name='agent_actions';

CREATE TABLE agent_actions_030 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    workspace_key TEXT NOT NULL,
    action_kind TEXT NOT NULL
        CHECK (action_kind IN (
            'save_conversation', 'save_web_source', 'create_workspace_item',
            'update_workspace_item', 'create_event', 'update_event',
            'create_workflow_run', 'confirm_source_candidate',
            'confirm_relation_candidate', 'update_mastery', 'save_memory_block'
        )),
    target_type TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    proposal_json TEXT NOT NULL DEFAULT '{}',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'approved', 'rejected', 'applied', 'failed', 'cancelled')),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    decided_at TEXT NOT NULL DEFAULT '',
    applied_at TEXT NOT NULL DEFAULT '',
    approved_at TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT
);

INSERT INTO agent_actions_030 (
    id, run_id, workspace_key, action_kind, target_type, target_id,
    proposal_json, before_json, after_json, status, error, created_at,
    decided_at, applied_at, approved_at, attempt, lease_expires_at
)
SELECT id, run_id, workspace_key, action_kind, target_type, target_id,
       proposal_json, before_json, after_json, status, error, created_at,
       decided_at, applied_at, approved_at, attempt, lease_expires_at
FROM agent_actions;

DROP TABLE agent_actions;
ALTER TABLE agent_actions_030 RENAME TO agent_actions;

INSERT INTO sqlite_sequence(name, seq)
    SELECT 'agent_actions', seq FROM agent_actions_030_sequence
    WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='agent_actions');
UPDATE sqlite_sequence
    SET seq=MAX(seq, (SELECT seq FROM agent_actions_030_sequence))
    WHERE name='agent_actions';
DROP TABLE agent_actions_030_sequence;

CREATE INDEX IF NOT EXISTS idx_agent_actions_run
    ON agent_actions(run_id, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_agent_actions_target
    ON agent_actions(workspace_key, action_kind, target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_agent_actions_lease
    ON agent_actions(status, lease_expires_at);
