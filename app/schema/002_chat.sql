-- ── 对话：会话与消息 ──────────────────────────────────
-- 「对话是主界面」不能只活在前端内存里——刷新就没了等于没记。
-- 会话归属于一个检索范围（'*' 全部课程 / 课名），消息保留检索与引用痕迹。

CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT DEFAULT '新对话',
    scope       TEXT DEFAULT '*',         -- '*' = 全部课程，否则课名
    role_key    TEXT DEFAULT 'laoba',     -- 本会话固定的角色
    tier        TEXT DEFAULT 'fast',      -- fast / reasoning
    created_at  TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id         INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,        -- user / assistant
    content         TEXT NOT NULL,        -- 正文（assistant 为 Markdown）
    role_name       TEXT DEFAULT '',      -- 作答角色名，如「老巴助教」
    scope_label     TEXT DEFAULT '',      -- 本轮范围标签
    steps_json      TEXT DEFAULT '[]',    -- 检索/路由步骤，原样回放
    citations_json  TEXT DEFAULT '[]',    -- 引用 [{id,name,course_name}]
    writeback_json  TEXT DEFAULT '[]',    -- 建议写回的条目
    engine          TEXT DEFAULT 'local', -- local 本地拼装 / model 走了模型
    error           TEXT DEFAULT '',      -- 走模型失败时的原因
    created_at      TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, id);
CREATE INDEX IF NOT EXISTS idx_conv_upd ON conversations(updated_at DESC);
