-- Canonical textbook structure. Chapters are navigation structure, not KP nodes.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS chapter_catalog (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id     INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    source_id     INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    chapter_key   TEXT NOT NULL,
    number        INTEGER NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    start_locator INTEGER,
    end_locator   INTEGER,
    source_sha256 TEXT NOT NULL DEFAULT '',
    catalog_version TEXT NOT NULL DEFAULT '2',
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(course_id, chapter_key)
);

CREATE INDEX IF NOT EXISTS idx_chapter_catalog_course
    ON chapter_catalog(course_id, number);

CREATE TABLE IF NOT EXISTS chapter_sections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id    INTEGER NOT NULL REFERENCES chapter_catalog(id) ON DELETE CASCADE,
    section_key   TEXT NOT NULL,
    number        TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    start_locator INTEGER,
    end_locator   INTEGER,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(chapter_id, section_key)
);

CREATE INDEX IF NOT EXISTS idx_chapter_sections_chapter
    ON chapter_sections(chapter_id, number);
