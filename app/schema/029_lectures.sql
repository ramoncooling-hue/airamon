-- 0.6.2 / 旁听转录 v0: lecture_sessions + lecture_segments (029).
--
-- 一次「旁听」= 一段被转写的课堂/会议音频。会话行保存来源（麦克风/系统
-- 声音/导入音频）、模型、状态与沉淀出的知识库文档；片段表保存逐句字幕
-- （start_ms/end_ms 为会话内绝对毫秒，与 app/memory/asr.StreamSession 对齐）。
--
-- 音频本体不落库：v0 只保留文字记录（隐私优先，也避免每小时上百 MB 的
-- 音频文件），后续如加留存另开列。
--
-- 幂等：客户端可传 idempotency_key（刷新/重试时同一次旁听只落一行），
-- 空字符串必须可复用，因此用部分唯一索引 WHERE idempotency_key != ''。
--
-- 时间一律本地时间，与库内其它表一致；discarded 为软删（保留审计痕迹，
-- 但不出现在列表与统计里）。
CREATE TABLE IF NOT EXISTS lecture_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER REFERENCES workspaces(id) ON DELETE SET NULL,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    title TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'mic' CHECK(source IN ('mic','system','upload')),
    status TEXT NOT NULL DEFAULT 'live' CHECK(status IN ('live','ended','discarded')),
    model TEXT NOT NULL DEFAULT 'turbo',
    started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    ended_at TEXT NOT NULL DEFAULT '',
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    document_id INTEGER REFERENCES knowledge_documents(id) ON DELETE SET NULL,
    notes TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS lecture_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES lecture_sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL DEFAULT 0,
    start_ms INTEGER NOT NULL DEFAULT 0,
    end_ms INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_lecture_sessions_workspace
    ON lecture_sessions(workspace_id, started_at);
CREATE INDEX IF NOT EXISTS idx_lecture_sessions_course
    ON lecture_sessions(course_id, started_at);
CREATE INDEX IF NOT EXISTS idx_lecture_segments_session
    ON lecture_segments(session_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lecture_sessions_idempotency
    ON lecture_sessions(idempotency_key) WHERE idempotency_key != '';
