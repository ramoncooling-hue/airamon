-- CyberPal · 记忆地基
--
-- 设计依据：docs/ARCHITECTURE.md
-- 三条判别线决定表结构：
--   检索即记忆 → 每条记忆要能被检索（索引对象是知识点级，不是材料级）
--   回答带引用 → 每条记忆自带出处（source 字段是硬要求，不是可选）
--   结论能写回 → 记忆有可变状态（mastery），且写回留痕（revisions）

PRAGMA foreign_keys = ON;

-- ── 课程：记忆的容器 ────────────────────────────────
CREATE TABLE IF NOT EXISTS courses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    teacher     TEXT DEFAULT '',
    classroom   TEXT DEFAULT '',
    schedule    TEXT DEFAULT '',          -- 上课时间描述
    nature      TEXT DEFAULT '',          -- 核心课 / 选修课
    credits     REAL DEFAULT 0,
    assessment  TEXT DEFAULT '',          -- 考核方式
    note        TEXT DEFAULT '',          -- 备注（如缺课规则）
    status      TEXT DEFAULT '在读',       -- 在读 / 待补
    sort_order  INTEGER DEFAULT 0
);

-- ── 记忆单元：知识点 ────────────────────────────────
-- 这是整个系统的核心资产。字段来自 seed/ 的公开初始口径。
CREATE TABLE IF NOT EXISTS kp (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id   INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    record_id   TEXT,                     -- 原库 record_id，便于回溯
    name        TEXT NOT NULL,            -- 知识点
    band        TEXT DEFAULT '',          -- 语义带（图谱分带用）
    chapter     TEXT DEFAULT '',          -- 章节 / 讲次
    gist        TEXT DEFAULT '',          -- 一句话
    detail      TEXT DEFAULT '',          -- 详解
    trap        TEXT DEFAULT '',          -- 易错点
    source      TEXT DEFAULT '',          -- 溯源 ← 「回答带引用」的凭据
    related     TEXT DEFAULT '',          -- 关联
    kp_type     TEXT DEFAULT '',          -- 概念 / 考点 / 模型 / 方法 / 案例
    freq        TEXT DEFAULT '',          -- 高频 / 中频 / 低频
    mastery     TEXT DEFAULT '未读',       -- 熟练 / 会用 / 读过 / 未读 ← 「结论能写回」的目标
    updated_at  TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(course_id, name)
);

CREATE INDEX IF NOT EXISTS idx_kp_course  ON kp(course_id);
CREATE INDEX IF NOT EXISTS idx_kp_mastery ON kp(mastery);
CREATE INDEX IF NOT EXISTS idx_kp_freq    ON kp(freq);

-- ── 记忆的结构：知识点之间的关系 ──────────────────────
-- kind: pre 前置 / related 关联 / bridge 跨课桥接
CREATE TABLE IF NOT EXISTS kp_edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_kp     INTEGER NOT NULL REFERENCES kp(id) ON DELETE CASCADE,
    to_kp       INTEGER NOT NULL REFERENCES kp(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL DEFAULT 'pre',
    created_at  TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(from_kp, to_kp, kind)
);

CREATE INDEX IF NOT EXISTS idx_edge_from ON kp_edges(from_kp);
CREATE INDEX IF NOT EXISTS idx_edge_to   ON kp_edges(to_kp);

-- ── 跨课桥接：知识点 A 与它课知识点 B 的主题关系 ────────
-- 与 kp_edges 分开：桥接的两端可能有一端不在本课库里（如 DMD 课无数据）
CREATE TABLE IF NOT EXISTS bridges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_a    TEXT NOT NULL,
    kp_a        TEXT NOT NULL,
    course_b    TEXT NOT NULL,
    kp_b        TEXT NOT NULL,
    topic       TEXT DEFAULT '',          -- 桥接主题
    logic       TEXT DEFAULT '',          -- 桥接逻辑
    purpose     TEXT DEFAULT '',          -- 用途（考试串联 / 论文素材）
    updated_at  TEXT DEFAULT ''
);

-- ── 记忆的进度：本课每次课的推进 ────────────────────
CREATE TABLE IF NOT EXISTS progress (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id   INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    record_id   TEXT,
    seq         TEXT DEFAULT '',          -- 第 N 次（面授）
    topic       TEXT DEFAULT '',          -- 主题
    homework    TEXT DEFAULT '',          -- 作业
    note        TEXT DEFAULT '',          -- 备注
    due_at      TEXT DEFAULT '',          -- 截止
    submit_via  TEXT DEFAULT '',          -- 提交方式
    status      TEXT DEFAULT '',          -- 已提交 / 未提交
    date        TEXT DEFAULT '',          -- 日期
    prereq      TEXT DEFAULT ''           -- 预习要求
);

CREATE INDEX IF NOT EXISTS idx_prog_course ON progress(course_id);

-- ── 记忆的题库：与知识点绑定的自测题 ──────────────────
CREATE TABLE IF NOT EXISTS quiz (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id   INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    kp_id       INTEGER REFERENCES kp(id) ON DELETE SET NULL,
    record_id   TEXT,
    kp_name     TEXT DEFAULT '',          -- 关联知识点名（原库口径）
    source      TEXT DEFAULT '',          -- 来源
    stem        TEXT DEFAULT '',          -- 题干
    answer      TEXT DEFAULT '',          -- 答案
    analysis    TEXT DEFAULT '',          -- 解析
    difficulty  TEXT DEFAULT ''           -- 基础 / 综合 / 挑战
);

CREATE INDEX IF NOT EXISTS idx_quiz_kp ON quiz(kp_id);

-- ── 记忆的时间投影：日程 ────────────────────────────
-- category: class 上课 / hw 作业 / exam 考试 / personal 个人
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id   TEXT,
    course_id   INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    course_name TEXT DEFAULT '',
    title       TEXT DEFAULT '',
    category    TEXT DEFAULT 'class',
    type_raw    TEXT DEFAULT '',          -- 原库「类型」原值
    date        TEXT DEFAULT '',
    time_range  TEXT DEFAULT '',          -- 14:00–17:20
    location    TEXT DEFAULT '',
    note        TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_event_date ON events(date);
CREATE INDEX IF NOT EXISTS idx_event_cat  ON events(category);

-- ── 写回留痕：谁在什么时候改了什么 ────────────────────
-- 「结论能写回」要可追溯，否则记忆不可信
CREATE TABLE IF NOT EXISTS revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kp_id       INTEGER REFERENCES kp(id) ON DELETE CASCADE,
    field       TEXT NOT NULL,            -- mastery / gist / trap ...
    old_value   TEXT DEFAULT '',
    new_value   TEXT DEFAULT '',
    origin      TEXT DEFAULT '',          -- chat 对话写回 / drop 投放 / manual 手改
    confirmed   INTEGER DEFAULT 1,        -- 是否经人确认
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_rev_kp ON revisions(kp_id);
