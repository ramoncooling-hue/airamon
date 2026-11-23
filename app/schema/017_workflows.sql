-- Lightweight user-authored workflow definitions and execution history.
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    workspace_key TEXT NOT NULL DEFAULT 'studypal', description TEXT NOT NULL DEFAULT '',
    trigger_json TEXT NOT NULL DEFAULT '{}', version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','enabled','disabled')),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS workflow_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    key TEXT NOT NULL, position INTEGER NOT NULL CHECK(position >= 0),
    kind TEXT NOT NULL CHECK(kind IN ('input','agent','skill','approval')), name TEXT NOT NULL DEFAULT '',
    agent_key TEXT NOT NULL DEFAULT '', skill_key TEXT NOT NULL DEFAULT '', config_json TEXT NOT NULL DEFAULT '{}',
    input_map_json TEXT NOT NULL DEFAULT '{}', output_schema_json TEXT NOT NULL DEFAULT '{}',
    on_error TEXT NOT NULL DEFAULT 'pause' CHECK(on_error IN ('pause','retry_once','skip')),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')), updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(workflow_id, key), UNIQUE(workflow_id, position)
);
CREATE INDEX IF NOT EXISTS idx_workflow_steps_order ON workflow_steps(workflow_id, position);
CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    trigger_type TEXT NOT NULL DEFAULT 'manual', input_json TEXT NOT NULL DEFAULT '{}', context_json TEXT NOT NULL DEFAULT '{}',
    current_step_id INTEGER REFERENCES workflow_steps(id) ON DELETE SET NULL, output_json TEXT NOT NULL DEFAULT '{}',
    duration_ms INTEGER NOT NULL DEFAULT 0 CHECK(duration_ms >= 0),
    status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','waiting_approval','success','error','cancelled')),
    started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')), finished_at TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow ON workflow_runs(workflow_id, id DESC);
CREATE TABLE IF NOT EXISTS workflow_run_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    step_id INTEGER NOT NULL REFERENCES workflow_steps(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','waiting_approval','success','error','skipped')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0), input_json TEXT NOT NULL DEFAULT '{}', output_json TEXT NOT NULL DEFAULT '{}',
    duration_ms INTEGER NOT NULL DEFAULT 0 CHECK(duration_ms >= 0), error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '', finished_at TEXT NOT NULL DEFAULT '', UNIQUE(run_id, step_id)
);
CREATE INDEX IF NOT EXISTS idx_workflow_run_steps_status ON workflow_run_steps(run_id, status, id);
