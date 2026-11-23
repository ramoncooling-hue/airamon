-- Local full-text index. It is deliberately SQLite-only so the source bundle
-- remains portable and does not require a vector service.
PRAGMA foreign_keys = ON;

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    kind UNINDEXED,
    ref_id UNINDEXED,
    course_name,
    title,
    body,
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS search_fts_kp_insert AFTER INSERT ON kp BEGIN
    INSERT INTO search_fts(kind, ref_id, course_name, title, body)
    SELECT 'kp', CAST(new.id AS TEXT), co.name, new.name,
           coalesce(new.gist,'') || ' ' || coalesce(new.detail,'') || ' ' ||
           coalesce(new.trap,'') || ' ' || coalesce(new.related,'')
      FROM courses co WHERE co.id = new.course_id;
END;
CREATE TRIGGER IF NOT EXISTS search_fts_kp_update AFTER UPDATE ON kp BEGIN
    DELETE FROM search_fts WHERE kind='kp' AND ref_id=CAST(old.id AS TEXT);
    INSERT INTO search_fts(kind, ref_id, course_name, title, body)
    SELECT 'kp', CAST(new.id AS TEXT), co.name, new.name,
           coalesce(new.gist,'') || ' ' || coalesce(new.detail,'') || ' ' ||
           coalesce(new.trap,'') || ' ' || coalesce(new.related,'')
      FROM courses co WHERE co.id = new.course_id;
END;
CREATE TRIGGER IF NOT EXISTS search_fts_kp_delete AFTER DELETE ON kp BEGIN
    DELETE FROM search_fts WHERE kind='kp' AND ref_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER IF NOT EXISTS search_fts_chunk_insert AFTER INSERT ON source_chunks BEGIN
    INSERT INTO search_fts(kind, ref_id, course_name, title, body)
    SELECT 'source_chunk', CAST(new.id AS TEXT), co.name, sf.path, new.content
      FROM source_files sf JOIN courses co ON co.id=sf.course_id
     WHERE sf.id=new.source_id;
END;
CREATE TRIGGER IF NOT EXISTS search_fts_chunk_update AFTER UPDATE ON source_chunks BEGIN
    DELETE FROM search_fts WHERE kind='source_chunk' AND ref_id=CAST(old.id AS TEXT);
    INSERT INTO search_fts(kind, ref_id, course_name, title, body)
    SELECT 'source_chunk', CAST(new.id AS TEXT), co.name, sf.path, new.content
      FROM source_files sf JOIN courses co ON co.id=sf.course_id
     WHERE sf.id=new.source_id;
END;
CREATE TRIGGER IF NOT EXISTS search_fts_chunk_delete AFTER DELETE ON source_chunks BEGIN
    DELETE FROM search_fts WHERE kind='source_chunk' AND ref_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER IF NOT EXISTS search_fts_evidence_insert AFTER INSERT ON evidence BEGIN
    INSERT INTO search_fts(kind, ref_id, course_name, title, body)
    SELECT 'evidence', CAST(new.id AS TEXT), co.name,
           sf.path || ' ' || coalesce(new.locator_type,''), new.content
      FROM source_files sf JOIN courses co ON co.id=sf.course_id
     WHERE sf.id=new.source_id;
END;
CREATE TRIGGER IF NOT EXISTS search_fts_evidence_update AFTER UPDATE ON evidence BEGIN
    DELETE FROM search_fts WHERE kind='evidence' AND ref_id=CAST(old.id AS TEXT);
    INSERT INTO search_fts(kind, ref_id, course_name, title, body)
    SELECT 'evidence', CAST(new.id AS TEXT), co.name,
           sf.path || ' ' || coalesce(new.locator_type,''), new.content
      FROM source_files sf JOIN courses co ON co.id=sf.course_id
     WHERE sf.id=new.source_id;
END;
CREATE TRIGGER IF NOT EXISTS search_fts_evidence_delete AFTER DELETE ON evidence BEGIN
    DELETE FROM search_fts WHERE kind='evidence' AND ref_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER IF NOT EXISTS search_fts_event_insert AFTER INSERT ON events BEGIN
    INSERT INTO search_fts(kind, ref_id, course_name, title, body)
    VALUES('event', CAST(new.id AS TEXT), new.course_name, new.title,
           coalesce(new.note,'') || ' ' || coalesce(new.category,'') || ' ' || coalesce(new.date,''));
END;
CREATE TRIGGER IF NOT EXISTS search_fts_event_update AFTER UPDATE ON events BEGIN
    DELETE FROM search_fts WHERE kind='event' AND ref_id=CAST(old.id AS TEXT);
    INSERT INTO search_fts(kind, ref_id, course_name, title, body)
    VALUES('event', CAST(new.id AS TEXT), new.course_name, new.title,
           coalesce(new.note,'') || ' ' || coalesce(new.category,'') || ' ' || coalesce(new.date,''));
END;
CREATE TRIGGER IF NOT EXISTS search_fts_event_delete AFTER DELETE ON events BEGIN
    DELETE FROM search_fts WHERE kind='event' AND ref_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER IF NOT EXISTS search_fts_notice_insert AFTER INSERT ON notices BEGIN
    INSERT INTO search_fts(kind, ref_id, course_name, title, body)
    VALUES('notice', CAST(new.id AS TEXT), new.course_name, new.title,
           coalesce(new.body,'') || ' ' || coalesce(new.source,'') || ' ' || coalesce(new.date,''));
END;
CREATE TRIGGER IF NOT EXISTS search_fts_notice_update AFTER UPDATE ON notices BEGIN
    DELETE FROM search_fts WHERE kind='notice' AND ref_id=CAST(old.id AS TEXT);
    INSERT INTO search_fts(kind, ref_id, course_name, title, body)
    VALUES('notice', CAST(new.id AS TEXT), new.course_name, new.title,
           coalesce(new.body,'') || ' ' || coalesce(new.source,'') || ' ' || coalesce(new.date,''));
END;
CREATE TRIGGER IF NOT EXISTS search_fts_notice_delete AFTER DELETE ON notices BEGIN
    DELETE FROM search_fts WHERE kind='notice' AND ref_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER IF NOT EXISTS search_fts_progress_insert AFTER INSERT ON progress BEGIN
    INSERT INTO search_fts(kind, ref_id, course_name, title, body)
    SELECT 'progress', CAST(new.id AS TEXT), co.name, new.topic,
           coalesce(new.homework,'') || ' ' || coalesce(new.note,'') || ' ' || coalesce(new.due_at,'')
      FROM courses co WHERE co.id=new.course_id;
END;
CREATE TRIGGER IF NOT EXISTS search_fts_progress_update AFTER UPDATE ON progress BEGIN
    DELETE FROM search_fts WHERE kind='progress' AND ref_id=CAST(old.id AS TEXT);
    INSERT INTO search_fts(kind, ref_id, course_name, title, body)
    SELECT 'progress', CAST(new.id AS TEXT), co.name, new.topic,
           coalesce(new.homework,'') || ' ' || coalesce(new.note,'') || ' ' || coalesce(new.due_at,'')
      FROM courses co WHERE co.id=new.course_id;
END;
CREATE TRIGGER IF NOT EXISTS search_fts_progress_delete AFTER DELETE ON progress BEGIN
    DELETE FROM search_fts WHERE kind='progress' AND ref_id=CAST(old.id AS TEXT);
END;
