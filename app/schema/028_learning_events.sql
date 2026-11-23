-- T3.7 / O-LEARN-3 (B3): learning events foundation (028).
--
-- Three append-only tables record what the learner did: learning_sessions (a
-- study/review/read/chat sitting), learning_events (the atomic action) and
-- quiz_attempts (one answer of one quiz item). They are new tables only: no
-- existing table is altered and no FTS trigger is added, because learning
-- events are not part of full-text search (B3-EXECUTION-BRIEF §3).
--
-- Idempotency: a client may pass an optional idempotency_key so a retried
-- POST lands once. Empty strings must stay reusable, so uniqueness is a
-- partial unique index (WHERE idempotency_key != '') instead of a table-level
-- UNIQUE: in SQLite NULLs are distinct but '' is not, and a table-level UNIQUE
-- would let every second event without a key collide.
--
-- All timestamps are local time, matching the rest of the database.
CREATE TABLE IF NOT EXISTS learning_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER REFERENCES workspaces(id) ON DELETE SET NULL,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    kind TEXT NOT NULL DEFAULT 'study' CHECK(kind IN ('study','review','read','chat')),
    started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    ended_at TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    idempotency_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS learning_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES learning_sessions(id) ON DELETE SET NULL,
    workspace_id INTEGER REFERENCES workspaces(id) ON DELETE SET NULL,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    kp_id INTEGER REFERENCES kp(id) ON DELETE SET NULL,
    document_id INTEGER REFERENCES knowledge_documents(id) ON DELETE SET NULL,
    source_id INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK(kind IN ('read','open_kp','open_material','excerpt','note_saved',
                                      'candidate_confirmed','candidate_rejected','review_item',
                                      'quiz_attempt','mastery_changed','chat_ask')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    idempotency_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS quiz_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_id INTEGER REFERENCES quiz(id) ON DELETE SET NULL,
    kp_id INTEGER REFERENCES kp(id) ON DELETE SET NULL,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    result TEXT NOT NULL CHECK(result IN ('right','wrong','skip')),
    answer TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Contracted access paths: recent events, per-course timelines and review
-- lookups (review.py reads kind='review_item' by kp_id).
CREATE INDEX IF NOT EXISTS idx_learning_events_occurred
    ON learning_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_learning_events_course
    ON learning_events(course_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_learning_events_kp
    ON learning_events(kp_id);
-- Summary filters one workspace by time; without this index every card scan
-- walks the whole event history.
CREATE INDEX IF NOT EXISTS idx_learning_events_workspace
    ON learning_events(workspace_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_learning_sessions_started
    ON learning_sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_quiz_attempts_kp
    ON quiz_attempts(kp_id);

-- Retry-safe writes: one row per non-empty key, unlimited key-less rows.
CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_sessions_idempotency
    ON learning_sessions(idempotency_key) WHERE idempotency_key != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_events_idempotency
    ON learning_events(idempotency_key) WHERE idempotency_key != '';
