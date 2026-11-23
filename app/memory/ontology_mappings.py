"""Immutable source-scoped mapping drafts and compare-and-set human review.

Bases reference approved/superseded versions in the same source scope. To revise
a draft or rejected proposal, pass its original base_version_id, not its id.
Repeated review transitions, including approval, raise MappingConflict.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import closing

from . import db, domain_profiles, mapping_evidence, ontology


class MappingNotFound(ValueError):
    """No mapping with this identity exists in the requested workspace."""


class MappingConflict(ValueError):
    """The proposal, review state, evidence, or approval baseline changed."""


def _text(value, field, *, limit=256, blank=False):
    if type(value) is not str or len(value) > limit or (not blank and not value.strip()):
        raise ValueError(f'{field} must be a bounded string')
    return value.strip()


def _version_id(value):
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise ValueError('version_id must be a positive integer')
    return value


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                          allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError('Mapping content must be JSON serializable') from None


def _hash(value):
    try:
        return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()
    except UnicodeError:
        raise ValueError('Mapping content must be valid Unicode') from None


def _workspace(conn, workspace_key):
    row = conn.execute('SELECT kind,status FROM workspaces WHERE key=?',
                       (workspace_key,)).fetchone()
    if row is None:
        raise MappingNotFound('Workspace is unavailable')
    if row['status'] != 'active':
        raise MappingConflict('Workspace is no longer active')
    return row['kind']


def _normalize(mapping, workspace_kind):
    if type(mapping) is not dict:
        raise ValueError('mapping must be a JSON object')
    _json(mapping)
    result = ontology.validate_mapping(mapping, workspace_kind=workspace_kind)
    result.pop('status')
    for field in ('branch_keys', 'node_types', 'relation_kinds', 'sample_refs'):
        result[field] = sorted(result[field])
    return result


def _refresh_profiles():
    # Profile files are the versioned review contract. Do not let the process
    # cache hide a same-version edit between draft creation and approval.
    clear = getattr(domain_profiles.load_profiles, 'cache_clear', None)
    if clear is not None:
        clear()


def _profile_hash(mapping):
    return _hash(domain_profiles.load_profiles()[mapping['profile_id']])


def _scope_hash(bindings):
    return _hash(sorted({item['source_id'] for item in bindings}))


def _mapping_hash(mapping, bindings, profile_hash, base_version_id):
    return _hash({'mapping': mapping, 'bindings': bindings, 'profile_hash': profile_hash,
                  'base_version_id': base_version_id})


def _row(conn, version_id, workspace_key):
    row = conn.execute('SELECT * FROM ontology_mapping_versions WHERE id=? AND workspace_key=?',
                       (_version_id(version_id), workspace_key)).fetchone()
    if row is None:
        raise MappingNotFound('Mapping version not found in workspace')
    return row


def _public(row):
    result = dict(row)
    result['mapping'] = json.loads(result.pop('mapping_json'))
    result['bindings'] = json.loads(result.pop('bindings_json'))
    return result


def _checked_row(conn, version_id, workspace_key, expected_hash, status):
    row = _row(conn, version_id, workspace_key)
    if type(expected_hash) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_hash):
        raise ValueError('expected_hash must be a lowercase SHA256 digest')
    if row['mapping_hash'] != expected_hash or row['status'] != status:
        raise MappingConflict('Mapping hash or review state changed')
    return row


def _revalidate(conn, row):
    try:
        _refresh_profiles()
        kind = _workspace(conn, row['workspace_key'])
        mapping = json.loads(row['mapping_json'])
        normalized = _normalize(mapping, kind)
        bindings = json.loads(row['bindings_json'])
        profile_hash = _profile_hash(normalized)
        if (normalized != mapping or profile_hash != row['profile_hash']
                or any(normalized[key] != row[key] for key in (
                    'ontology_version', 'profile_id', 'profile_version'))
                or _scope_hash(bindings) != row['source_scope_hash']
                or _mapping_hash(mapping, bindings, profile_hash, row['base_version_id'])
                != row['mapping_hash']):
            raise ValueError('Mapping contract changed')
        mapping_evidence.verify_bindings(conn, row['workspace_key'], bindings)
        # Classification is mutable independently of snapshot hashes. Apply
        # this at every review transition, including legacy/manual proposals.
        source_ids = sorted({item['source_id'] for item in bindings})
        if conn.execute(
            f"SELECT 1 FROM source_files WHERE id IN ({','.join('?' for _ in source_ids)}) "
            "AND source_kind='development_doc' LIMIT 1", source_ids).fetchone():
            raise ValueError('Development documents cannot support mapping approval')
    except MappingNotFound:
        raise
    except ValueError:
        raise MappingConflict('Mapping contract, workspace, or evidence changed; create a new draft') from None


def _source_state_hash(conn, source_id):
    """Hash rescan/ownership state, including unsampled evidence, without paths.

    A source hash alone cannot detect re-extraction under the same file hash.
    Stream rows into the digest so the precondition does not retain source text.
    The caller must hold its read snapshot or the creation write transaction.
    """
    digest = hashlib.sha256()
    queries = (
        ('''SELECT sf.id,sf.course_id,sf.sha256,sf.status,sf.source_kind,sf.indexed_at,
                   c.workspace_id,w.key,w.kind,w.status
            FROM source_files sf JOIN courses c ON c.id=sf.course_id
            LEFT JOIN workspaces w ON w.id=c.workspace_id WHERE sf.id=?''', (source_id,)),
        ('SELECT id,sha256,extracted_at FROM source_snapshots WHERE source_id=? ORDER BY id', (source_id,)),
        ('''SELECT id,snapshot_id,stale,content,locator_type,locator_start,locator_end
            FROM evidence WHERE source_id=? ORDER BY id''', (source_id,)),
    )
    for sql, args in queries:
        digest.update(b'\n')
        for row in conn.execute(sql, args):
            digest.update(_json(list(row)).encode('utf-8'))
            digest.update(b'\n')
    return digest.hexdigest()


def create_mapping(workspace_key, mapping, created_by='user', *, base_version_id=None,
                   expected_bindings=None, expected_profile_hash=None, expected_source_hash=None):
    """Validate and capture a new draft, or return its existing immutable version."""
    workspace_key = _text(workspace_key, 'workspace_key')
    created_by = _text(created_by, 'created_by')
    if base_version_id is not None:
        _version_id(base_version_id)
    with closing(db.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        _refresh_profiles()
        try:
            kind = _workspace(conn, workspace_key)
            # Compare before validation: removal/version changes must also be
            # conflicts for an in-flight generation, not new invalid proposals.
            if expected_profile_hash is not None and _profile_hash(mapping) != expected_profile_hash:
                raise MappingConflict('Profile changed during generation')
            normalized = _normalize(mapping, kind)
            bindings = mapping_evidence.capture_bindings(conn, workspace_key, normalized['sample_refs'])
        except (ValueError, KeyError):
            if expected_bindings is not None or expected_profile_hash is not None:
                raise MappingConflict('Generation contract or evidence changed') from None
            raise
        bindings = sorted(bindings, key=lambda item: item['evidence_id'])
        if expected_bindings is not None:
            mapping_evidence.verify_bindings(conn, workspace_key, bindings)
            if _json(bindings) != _json(expected_bindings):
                raise MappingConflict('Evidence changed during generation')
        if expected_source_hash is not None:
            sources = {item['source_id'] for item in bindings}
            if len(sources) != 1 or _source_state_hash(conn, next(iter(sources))) != expected_source_hash:
                raise MappingConflict('Source was moved or re-extracted during generation')
        profile_hash = _profile_hash(normalized)
        scope_hash = _scope_hash(bindings)
        if expected_bindings is not None:
            active = conn.execute(
                """SELECT id FROM ontology_mapping_versions WHERE workspace_key=? AND profile_id=?
                   AND source_scope_hash=? AND status='approved'""",
                (workspace_key, normalized['profile_id'], scope_hash)).fetchone()
            if (active['id'] if active else None) != base_version_id:
                raise MappingConflict('Approved baseline changed during generation')
        if base_version_id is not None:
            base = _row(conn, base_version_id, workspace_key)
            if base['profile_id'] != normalized['profile_id'] or base['source_scope_hash'] != scope_hash:
                raise ValueError('Mapping base must have the same profile and source scope')
            if base['status'] not in ('approved', 'superseded'):
                raise MappingConflict('Mapping base must be approved or superseded')
        digest = _mapping_hash(normalized, bindings, profile_hash, base_version_id)
        existing = conn.execute(
            'SELECT * FROM ontology_mapping_versions WHERE workspace_key=? AND mapping_hash=?',
            (workspace_key, digest)).fetchone()
        if existing is not None:
            if expected_bindings is not None and existing['status'] != 'draft':
                raise MappingConflict('Identical proposal has already entered review')
            return _public(existing)
        cursor = conn.execute(
            '''INSERT INTO ontology_mapping_versions
               (workspace_key,ontology_version,profile_id,profile_version,profile_hash,
                source_scope_hash,mapping_json,bindings_json,mapping_hash,base_version_id,created_by)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (workspace_key, normalized['ontology_version'], normalized['profile_id'],
             normalized['profile_version'], profile_hash, scope_hash, _json(normalized),
             _json(bindings), digest, base_version_id, created_by))
        return _public(_row(conn, cursor.lastrowid, workspace_key))


def get_mapping(version_id, *, workspace_key):
    """Read an independent version, including historical archived-workspace rows."""
    workspace_key = _text(workspace_key, 'workspace_key')
    with closing(db.connect()) as conn:
        return _public(_row(conn, version_id, workspace_key))


def list_mappings(workspace_key, *, limit=50):
    """Read recent versions in one workspace without refreshing or writing data."""
    workspace_key = _text(workspace_key, 'workspace_key')
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('limit must be an integer between 1 and 1000')
    with closing(db.connect()) as conn:
        return [_public(row) for row in conn.execute(
            'SELECT * FROM ontology_mapping_versions WHERE workspace_key=? ORDER BY id DESC LIMIT ?',
            (workspace_key, limit))]


def submit_mapping(version_id, *, workspace_key, expected_hash):
    workspace_key = _text(workspace_key, 'workspace_key')
    with closing(db.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = _checked_row(conn, version_id, workspace_key, expected_hash, 'draft')
        _revalidate(conn, row)
        conn.execute("UPDATE ontology_mapping_versions SET status='pending_review' WHERE id=?", (version_id,))
        return _public(_row(conn, version_id, workspace_key))


def approve_mapping(version_id, approved_by, note='', *, workspace_key, expected_hash):
    """Approve once, atomically superseding only the reviewed source-scope base."""
    workspace_key = _text(workspace_key, 'workspace_key')
    approved_by = _text(approved_by, 'approved_by')
    note = _text(note, 'note', limit=4000, blank=True)
    with closing(db.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = _checked_row(conn, version_id, workspace_key, expected_hash, 'pending_review')
        _revalidate(conn, row)
        active = conn.execute(
            """SELECT id FROM ontology_mapping_versions WHERE workspace_key=? AND profile_id=?
               AND source_scope_hash=? AND status='approved'""",
            (workspace_key, row['profile_id'], row['source_scope_hash'])).fetchone()
        if (active['id'] if active is not None else None) != row['base_version_id']:
            raise MappingConflict('Active approved mapping changed; review a new draft against its base')
        if active is not None:
            conn.execute("UPDATE ontology_mapping_versions SET status='superseded' WHERE id=?", (active['id'],))
        conn.execute(
            """UPDATE ontology_mapping_versions SET status='approved',approved_by=?,
               approved_at=CURRENT_TIMESTAMP,note=? WHERE id=?""", (approved_by, note, version_id))
        return _public(_row(conn, version_id, workspace_key))


def reject_mapping(version_id, note='', *, workspace_key, expected_hash):
    """Reject a pending version even when its evidence has become unavailable."""
    workspace_key = _text(workspace_key, 'workspace_key')
    note = _text(note, 'note', limit=4000, blank=True)
    with closing(db.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        _checked_row(conn, version_id, workspace_key, expected_hash, 'pending_review')
        conn.execute(
            """UPDATE ontology_mapping_versions SET status='rejected',
               rejected_at=CURRENT_TIMESTAMP,note=? WHERE id=?""", (note, version_id))
        return _public(_row(conn, version_id, workspace_key))
