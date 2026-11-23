PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS user_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    display_name TEXT NOT NULL DEFAULT '',
    preferences_json TEXT NOT NULL DEFAULT '{}',
    memory_policy TEXT NOT NULL DEFAULT 'ask'
        CHECK (memory_policy IN ('ask', 'approved_types', 'all_local')),
    active_workspace_key TEXT NOT NULL DEFAULT 'studypal',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS agent_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_key TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'completed', 'archived')),
    priority INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    due_at TEXT NOT NULL DEFAULT '',
    next_action TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS agent_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_key TEXT NOT NULL,
    kind TEXT NOT NULL
        CHECK (kind IN ('source_arrived', 'event_due', 'workflow_waiting', 'goal_stale', 'manual')),
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    last_fired_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_key TEXT NOT NULL,
    agent_key TEXT NOT NULL,
    trigger_type TEXT NOT NULL DEFAULT 'manual',
    trigger_id TEXT NOT NULL DEFAULT '',
    input_json TEXT NOT NULL DEFAULT '{}',
    context_json TEXT NOT NULL DEFAULT '{}',
    dedupe_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'waiting_approval', 'succeeded', 'failed', 'cancelled')),
    summary TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0)
);

CREATE TABLE IF NOT EXISTS agent_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    workspace_key TEXT NOT NULL,
    action_kind TEXT NOT NULL
        CHECK (action_kind IN (
            'save_conversation', 'save_web_source', 'create_workspace_item',
            'update_workspace_item', 'create_event', 'update_event',
            'create_workflow_run', 'confirm_source_candidate',
            'confirm_relation_candidate', 'update_mastery'
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
    applied_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_agent_goals_workspace_status
    ON agent_goals(workspace_key, status, priority);
CREATE INDEX IF NOT EXISTS idx_agent_triggers_workspace_kind
    ON agent_triggers(workspace_key, kind, enabled);
CREATE INDEX IF NOT EXISTS idx_agent_triggers_last_fired
    ON agent_triggers(last_fired_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_workspace_status
    ON agent_runs(workspace_key, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_dedupe
    ON agent_runs(workspace_key, trigger_type, dedupe_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_nonempty_dedupe
    ON agent_runs(workspace_key, trigger_type, dedupe_key)
    WHERE dedupe_key <> '';
CREATE INDEX IF NOT EXISTS idx_agent_actions_run
    ON agent_actions(run_id, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_agent_actions_target
    ON agent_actions(workspace_key, action_kind, target_type, target_id);
