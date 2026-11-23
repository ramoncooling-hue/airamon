"""Snapshot bindings for mapping approval, using only scoped indexed evidence."""

import hashlib
import json
import re
import sqlite3


def capture_bindings(conn: sqlite3.Connection, workspace_key: str, sample_refs: list[str]) -> list[dict]:
    """Read a bounded source set inside the caller's transaction; never grant access."""
    if not isinstance(workspace_key, str) or not workspace_key:
        raise ValueError('A workspace is required')
    if type(sample_refs) is not list or not 1 <= len(sample_refs) <= 64:
        raise ValueError('Select 1..64 indexed evidence samples')
    ids = []
    for ref in sample_refs:
        if type(ref) is not str or not re.fullmatch(r'evidence:[1-9][0-9]{0,18}', ref):
            raise ValueError('Samples must use canonical evidence references')
        evidence_id = int(ref.split(':')[1])
        if evidence_id > 9223372036854775807 or evidence_id in ids:
            raise ValueError('Invalid or duplicate evidence reference')
        ids.append(evidence_id)
    workspace = conn.execute("SELECT id FROM workspaces WHERE key=? AND status='active'", (workspace_key,)).fetchone()
    if not workspace:
        raise ValueError('Workspace is unavailable')
    bindings = []
    for evidence_id in ids:
        row = conn.execute('''SELECT e.id,e.source_id,e.snapshot_id,e.content,e.locator_type,
                                    e.locator_start,e.locator_end,ss.sha256
                             FROM evidence e
                             JOIN source_files sf ON sf.id=e.source_id
                             JOIN courses c ON c.id=sf.course_id
                             JOIN source_snapshots ss ON ss.id=e.snapshot_id AND ss.source_id=sf.id
                             WHERE e.id=? AND e.stale=0 AND sf.status='indexed'
                               AND ss.sha256=sf.sha256
                               AND (c.workspace_id=? OR (c.workspace_id IS NULL AND ?='studypal'))''',
                           (evidence_id, workspace['id'], workspace_key)).fetchone()
        if not row or not row['content'].strip() or not re.fullmatch(r'[0-9a-f]{64}', row['sha256']):
            raise ValueError('Evidence is missing, stale, unversioned, or outside the workspace')
        evidence_payload = {key: row[key] for key in ('content', 'locator_type', 'locator_start', 'locator_end')}
        digest = hashlib.sha256(json.dumps(evidence_payload, sort_keys=True, ensure_ascii=False,
                                           separators=(',', ':')).encode('utf-8')).hexdigest()
        bindings.append({
            'evidence_id': row['id'], 'source_id': row['source_id'], 'snapshot_id': row['snapshot_id'],
            'source_sha256': row['sha256'], 'evidence_sha256': digest,
        })
    return bindings


def verify_bindings(conn: sqlite3.Connection, workspace_key: str, bindings: list[dict]) -> None:
    """Fail closed if any source, location, content, or ownership has changed."""
    keys = {'evidence_id', 'source_id', 'snapshot_id', 'source_sha256', 'evidence_sha256'}
    if type(bindings) is not list or not 1 <= len(bindings) <= 64:
        raise ValueError('Invalid evidence bindings')
    for binding in bindings:
        if type(binding) is not dict or binding.keys() != keys:
            raise ValueError('Invalid evidence binding')
        if any(type(binding[k]) is not int or binding[k] <= 0 for k in ('evidence_id', 'source_id', 'snapshot_id')):
            raise ValueError('Invalid evidence identity')
        if any(type(binding[k]) is not str or not re.fullmatch(r'[0-9a-f]{64}', binding[k])
               for k in ('source_sha256', 'evidence_sha256')):
            raise ValueError('Invalid evidence digest')
    current = capture_bindings(conn, workspace_key, [f"evidence:{item['evidence_id']}" for item in bindings])
    if current != bindings:
        raise ValueError('Evidence changed; create and review a new mapping version')
