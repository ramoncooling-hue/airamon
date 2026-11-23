-- Stable links for the legacy text-based cross-course bridge table.
CREATE TABLE IF NOT EXISTS bridge_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bridge_id INTEGER NOT NULL UNIQUE REFERENCES bridges(id) ON DELETE CASCADE,
    a_kp_id INTEGER REFERENCES kp(id) ON DELETE SET NULL,
    b_kp_id INTEGER REFERENCES kp(id) ON DELETE SET NULL,
    a_node_key TEXT NOT NULL DEFAULT '',
    b_node_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unresolved',
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_bridge_links_status ON bridge_links(status);
