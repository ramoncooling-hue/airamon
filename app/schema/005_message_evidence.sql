CREATE TABLE IF NOT EXISTS message_evidence (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
