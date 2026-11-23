"""Durable, explicit one-piece extraction from an approved mapping.

The ledger is a preview, never a publication channel. All model references are
segment-local; the host supplies immutable provenance. Reads project stale runs
as conflicts without updating them. Only explicit retry may replace a claim.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from contextlib import closing
from pathlib import Path

from ..agent import llm
from . import db, ontology_mappings

MAX_PIECE_CHARS = 4000
CLAIM_TIMEOUT_SECONDS = 120
_STALE = 'Mapping, profile, or source changed; create and review a new run'
_FAILED = 'Segment extraction failed; retry explicitly'
_STATUSES = ('pending', 'running', 'succeeded', 'failed', 'cancelled')


class ExtractionNotFound(ValueError):
    """An identity does not exist within the requested workspace."""


class ExtractionConflict(ValueError):
    """The approved contract changed, or a live claim cannot be retried."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _id(value):
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise ValueError('Identity must be a positive integer')
    return value


def _workspace(value):
    return ontology_mappings._text(value, 'workspace_key')


def _source_ids(value):
    if type(value) is not list or not 1 <= len(value) <= 64:
        raise ValueError('source_ids must be an explicit nonempty list of up to 64 identities')
    for item in value:
        _id(item)
    if len(set(value)) != len(value):
        raise ValueError('source_ids must not contain duplicates')
    return sorted(value)


def _mapping(conn, workspace_key, version_id, mapping_hash, source_ids, *, existing=False):
    try:
        row = ontology_mappings._checked_row(conn, version_id, workspace_key, mapping_hash, 'approved')
        ontology_mappings._revalidate(conn, row)
        bindings = json.loads(row['bindings_json'])
        if sorted({b['source_id'] for b in bindings}) != source_ids:
            raise ExtractionConflict(_STALE)
        return json.loads(row['mapping_json'])
    except ontology_mappings.MappingNotFound:
        if not existing:
            raise ExtractionNotFound('Mapping version not found in workspace') from None
        raise ExtractionConflict(_STALE) from None
    except (ValueError, KeyError, OSError, UnicodeError):
        raise ExtractionConflict(_STALE) from None


def _source_state(conn, source_ids):
    """Compare every indexed row, plus exact and resolved original file paths.

    Internal state contains digests only. Neither errors nor public responses
    expose local paths. Source text is streamed into the index digest.
    """
    result = []
    try:
        for source_id in source_ids:
            source = conn.execute('SELECT * FROM source_files WHERE id=?', (source_id,)).fetchone()
            if source is None or source['status'] != 'indexed' or source['source_kind'] == 'development_doc':
                raise ExtractionConflict(_STALE)
            path = Path(source['path']).expanduser()
            if not path.is_file():
                raise ExtractionConflict(_STALE)
            resolved = str(path.resolve(strict=True))
            digest = hashlib.sha256()
            with path.open('rb') as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b''):
                    digest.update(chunk)
            actual = digest.hexdigest()
            if actual != source['sha256']:
                raise ExtractionConflict(_STALE)
            index = hashlib.sha256()
            queries = (
                ('SELECT * FROM source_files WHERE id=?', (source_id,)),
                ('''SELECT c.id,c.workspace_id,w.key,w.kind,w.status FROM courses c
                    LEFT JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?''', (source['course_id'],)),
                ('SELECT * FROM source_snapshots WHERE source_id=? ORDER BY id', (source_id,)),
                ('SELECT * FROM evidence WHERE source_id=? ORDER BY id', (source_id,)),
                ('SELECT * FROM source_chunks WHERE source_id=? ORDER BY id', (source_id,)),
            )
            for sql, args in queries:
                index.update(b'\n')
                for row in conn.execute(sql, args):
                    index.update(_json(dict(row)).encode('utf-8'))
                    index.update(b'\n')
            result.append({'source_id': source_id, 'source_sha256': actual,
                           'path_sha256': _hash([source['path'], str(path.absolute()), resolved]),
                           'index_sha256': index.hexdigest()})
    except (OSError, ValueError, UnicodeError, RuntimeError):
        raise ExtractionConflict(_STALE) from None
    return result


def _row(conn, run_id, workspace_key):
    row = conn.execute('SELECT * FROM mapping_extraction_runs WHERE id=? AND workspace_key=?',
                       (run_id, workspace_key)).fetchone()
    if row is None:
        raise ExtractionNotFound('Extraction run not found in workspace')
    return row


def _current(conn, run):
    source_ids = json.loads(run['source_ids_json'])
    mapping = _mapping(conn, run['workspace_key'], run['mapping_version_id'], run['mapping_hash'],
                       source_ids, existing=True)
    if _json(_source_state(conn, source_ids)) != run['source_state_json']:
        raise ExtractionConflict(_STALE)
    return mapping


def _mark_conflict(conn, run):
    conn.execute("UPDATE mapping_extraction_runs SET status='conflict',error=? WHERE id=?", (_STALE, run['id']))
    conn.execute('''UPDATE mapping_extraction_segments SET status='cancelled',claim_token=NULL,
                    claimed_at=NULL,error=? WHERE run_id=? AND status IN ('pending','running')''',
                 (_STALE, run['id']))


def _checked_current(conn, run):
    try:
        return _current(conn, run)
    except ExtractionConflict:
        _mark_conflict(conn, run)
        return None


def _normal(value):
    return ' '.join((value or '').split())


def _statement(candidate):
    return {key: _normal(candidate.get(key))
            for key in ('name', 'kp_type', 'gist', 'detail',
                        'conditions', 'valid_from', 'valid_to')}


def _reference(run, segment, quote):
    return {**{key: segment[key] for key in ('evidence_id', 'source_id', 'snapshot_id', 'start', 'end',
                                           'source_sha256', 'evidence_sha256', 'segment_sha256', 'binding_hash')},
            'run_id': run['id'], 'segment_id': segment['id'], 'mapping_version_id': run['mapping_version_id'],
            'mapping_hash': run['mapping_hash'], 'quote': quote}


def _attach_refs(item, ref):
    if ref['segment_id'] not in item['segment_refs']:
        item['segment_refs'].append(ref['segment_id'])
    if ref not in item['evidence_refs']:
        item['evidence_refs'].append(ref)


def _aggregate(conn, run, segments):
    candidates, relations, conflicts, names = {}, {}, [], {}
    for segment in segments:
        local = {}
        for candidate in segment['candidates']:
            statement = _statement(candidate)
            name = statement['name'].casefold()
            statement['name'] = name
            identity = 'candidate:' + _hash(statement)
            local[candidate['id']] = identity
            if identity not in candidates:
                candidates[identity] = dict(candidate, id=identity, branch_keys=[], chapters=[],
                                            segment_refs=[], evidence_refs=[])
                names.setdefault(name, []).append(identity)
            item = candidates[identity]
            chapter = candidate.get('chapter', '')
            if chapter and chapter not in item['chapters']:
                item['chapters'].append(chapter)
            for branch in candidate.get('branch_keys', []):
                if branch not in item['branch_keys']:
                    item['branch_keys'].append(branch)
            _attach_refs(item, _reference(run, segment, candidate['quote']))
        for relation in segment['relations']:
            source, target = local[relation['from_id']], local[relation['to_id']]
            if source == target:
                conflicts.append({'kind': 'collapsed_relation', 'candidate_ids': [source],
                                  'relation': dict(relation), 'segment_refs': [segment['id']],
                                  'evidence_refs': [_reference(run, segment, relation['quote'])]})
                continue
            key = (source, target, relation['kind'])
            if key not in relations:
                relations[key] = dict(relation, from_id=source, to_id=target,
                                      segment_refs=[], evidence_refs=[])
            _attach_refs(relations[key], _reference(run, segment, relation['quote']))
    for name, identities in names.items():
        if len(identities) > 1:
            conflicts.append({'kind': 'same_name_variants', 'name': candidates[identities[0]]['name'],
                              'candidate_ids': identities})
    # Restrict collisions to the run's workspace. Legacy candidates may come
    # from other sources in that workspace; publication must review those too.
    for table, kind, join, clause in (
        ('knowledge_candidates', 'existing_candidate',
         'JOIN source_files sf ON sf.id=x.source_id JOIN courses c ON c.id=sf.course_id', ''),
        ('kp', 'existing_kp', 'JOIN courses c ON c.id=x.course_id', "AND COALESCE(x.deleted_at,'')=''"),
    ):
        if not names:
            break
        rows = conn.execute(f'''SELECT x.id,x.name FROM {table} x {join}
            LEFT JOIN workspaces w ON w.id=c.workspace_id
            WHERE (w.key=? OR (c.workspace_id IS NULL AND ?='studypal')) {clause} ORDER BY x.id''',
                            (run['workspace_key'], run['workspace_key']))
        for row in rows:
            name = _normal(row['name']).casefold()
            if name in names:
                conflicts.append({'kind': kind, 'name': candidates[names[name][0]]['name'],
                                  'candidate_ids': names[name], 'existing_id': row['id']})
    return list(candidates.values()), list(relations.values()), conflicts


def _detail(conn, run, *, current=None):
    stale = run['status'] == 'conflict'
    if not stale and current is None:
        try:
            _current(conn, run)
        except ExtractionConflict:
            stale = True
    segments = []
    coverage = dict(total=0, **dict.fromkeys(_STATUSES, 0))
    for row in conn.execute('SELECT * FROM mapping_extraction_segments WHERE run_id=? ORDER BY id', (run['id'],)):
        item = {key: row[key] for key in ('id', 'evidence_id', 'source_id', 'snapshot_id', 'start', 'end',
                                        'status', 'attempts', 'error', 'source_sha256', 'evidence_sha256',
                                        'segment_sha256', 'binding_hash')}
        output = json.loads(row['output_json']) if row['output_json'] and not stale else {}
        item.update(candidates=output.get('candidates', []), relations=output.get('relations', []))
        # Provenance is separate from model JSON and cannot be model-authored.
        item['provenance'] = _reference(run, item, '')
        segments.append(item)
        coverage['total'] += 1
        coverage[row['status']] += 1
    candidates, relations, conflicts = _aggregate(conn, run, segments) if not stale else ([], [], [])
    if stale:
        conflicts.insert(0, {'kind': 'stale_contract', 'message': _STALE})
    return {**{key: run[key] for key in ('id', 'workspace_key', 'mapping_version_id', 'mapping_hash')},
            'source_ids': json.loads(run['source_ids_json']), 'status': 'conflict' if stale else run['status'],
            'coverage': coverage, 'segments': segments, 'candidates': candidates,
            'relations': relations, 'conflicts': conflicts}


def create_run(*, workspace_key, mapping_version_id, mapping_hash, source_ids):
    """Create the full deterministic ledger without invoking a model; replay is exact."""
    workspace_key, mapping_version_id = _workspace(workspace_key), _id(mapping_version_id)
    source_ids = _source_ids(source_ids)
    if type(mapping_hash) is not str or not re.fullmatch(r'[0-9a-f]{64}', mapping_hash):
        raise ValueError('mapping_hash must be a lowercase SHA256 digest')
    with closing(db.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        mapping = _mapping(conn, workspace_key, mapping_version_id, mapping_hash, source_ids)
        state = _json(_source_state(conn, source_ids))
        # Read every live row first. Filtering joins would silently lose invalid
        # unsampled evidence and allow an incomplete ledger to finish.
        rows = conn.execute(f'''SELECT e.*,ss.source_id AS snapshot_source_id,
                ss.sha256 AS snapshot_sha256,sf.sha256 AS source_sha256 FROM evidence e
            LEFT JOIN source_snapshots ss ON ss.id=e.snapshot_id
            LEFT JOIN source_files sf ON sf.id=e.source_id
            WHERE e.source_id IN ({','.join('?' for _ in source_ids)}) AND e.stale=0
            ORDER BY e.id''', source_ids).fetchall()
        for evidence in rows:
            if (evidence['snapshot_id'] is None
                    or evidence['snapshot_source_id'] != evidence['source_id']
                    or evidence['snapshot_sha256'] != evidence['source_sha256']
                    or not evidence['content'].strip()):
                raise ExtractionConflict(_STALE)
        existing = conn.execute('''SELECT * FROM mapping_extraction_runs WHERE workspace_key=?
            AND mapping_version_id=? AND mapping_hash=? AND source_ids_json=?''',
                                (workspace_key, mapping_version_id, mapping_hash, _json(source_ids))).fetchone()
        if existing:
            if existing['source_state_json'] != state:
                raise ExtractionConflict(_STALE)
            return _detail(conn, existing, current=mapping)
        run_id = conn.execute('''INSERT INTO mapping_extraction_runs
            (workspace_key,mapping_version_id,mapping_hash,source_ids_json,source_state_json)
            VALUES(?,?,?,?,?)''', (workspace_key, mapping_version_id, mapping_hash, _json(source_ids), state)).lastrowid
        seen_sources = set()
        for evidence in rows:
            text = evidence['content']
            evidence_hash = _hash({key: evidence[key]
                                   for key in ('content', 'locator_type', 'locator_start', 'locator_end')})
            for start in range(0, len(text), MAX_PIECE_CHARS):
                end = min(start + MAX_PIECE_CHARS, len(text))
                piece = text[start:end]
                piece_hash = hashlib.sha256(piece.encode('utf-8')).hexdigest()
                binding = dict(run_id=run_id, mapping_version_id=mapping_version_id, mapping_hash=mapping_hash,
                               source_id=evidence['source_id'], snapshot_id=evidence['snapshot_id'],
                               evidence_id=evidence['id'], source_sha256=evidence['source_sha256'],
                               evidence_sha256=evidence_hash, segment_sha256=piece_hash, start=start, end=end,
                               source_state_sha256=hashlib.sha256(state.encode('utf-8')).hexdigest())
                conn.execute('''INSERT INTO mapping_extraction_segments
                    (run_id,evidence_id,source_id,snapshot_id,start,end,evidence_text,
                     source_sha256,evidence_sha256,segment_sha256,binding_hash)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                             (run_id, evidence['id'], evidence['source_id'], evidence['snapshot_id'], start, end,
                              piece, evidence['source_sha256'], evidence_hash, piece_hash, _hash(binding)))
                seen_sources.add(evidence['source_id'])
        if seen_sources != set(source_ids):
            raise ExtractionConflict('Source has no current indexed content')
        # Files are external to SQLite: check again before committing creation.
        if _json(_source_state(conn, source_ids)) != state:
            raise ExtractionConflict(_STALE)
        return _detail(conn, _row(conn, run_id, workspace_key), current=mapping)


def get_run(run_id, *, workspace_key):
    run_id, workspace_key = _id(run_id), _workspace(workspace_key)
    with closing(db.connect()) as conn:
        conn.execute('BEGIN')
        return _detail(conn, _row(conn, run_id, workspace_key))


def list_runs(*, workspace_key, mapping_version_id, limit=20):
    workspace_key, mapping_version_id = _workspace(workspace_key), _id(mapping_version_id)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('limit must be an integer between 1 and 1000')
    with closing(db.connect()) as conn:
        conn.execute('BEGIN')
        rows = conn.execute('''SELECT * FROM mapping_extraction_runs WHERE workspace_key=?
                            AND mapping_version_id=? ORDER BY id DESC LIMIT ?''',
                            (workspace_key, mapping_version_id, limit)).fetchall()
        return [_detail(conn, row) for row in rows]


def _status(conn, run_id):
    states = {row['status']: row['n'] for row in conn.execute('''SELECT status,COUNT(*) AS n
        FROM mapping_extraction_segments WHERE run_id=? GROUP BY status''', (run_id,))}
    if states.get('running'):
        status = 'running'
    elif states.get('failed'):
        status = 'partial'
    elif states.get('pending'):
        status = 'partial' if states.get('succeeded') else 'ready'
    elif states.get('cancelled'):
        status = 'cancelled'
    else:
        status = 'completed'
    conn.execute('UPDATE mapping_extraction_runs SET status=?,error=? WHERE id=?', (status, '', run_id))


def _claim_is_current(run_id, workspace_key, segment_id, token):
    with closing(db.connect()) as conn:
        return conn.execute('''SELECT 1 FROM mapping_extraction_segments s
            JOIN mapping_extraction_runs r ON r.id=s.run_id WHERE r.id=? AND r.workspace_key=?
            AND r.status='running' AND s.id=? AND s.status='running' AND s.claim_token=?''',
                            (run_id, workspace_key, segment_id, token)).fetchone() is not None


def step_run(run_id, *, workspace_key):
    """Claim at most one piece, call the model outside SQLite, then compare-and-set."""
    from . import extraction_output

    run_id, workspace_key = _id(run_id), _workspace(workspace_key)
    segment = None
    with closing(db.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        run = _row(conn, run_id, workspace_key)
        mapping = _checked_current(conn, run)
        if mapping is not None:
            if run['status'] in ('completed', 'cancelled', 'conflict'):
                return _detail(conn, run, current=mapping)
            live = conn.execute("SELECT 1 FROM mapping_extraction_segments WHERE run_id=? AND status='running'",
                                (run_id,)).fetchone()
            if not live:
                segment = conn.execute('''SELECT * FROM mapping_extraction_segments
                    WHERE run_id=? AND status='pending' ORDER BY id LIMIT 1''', (run_id,)).fetchone()
            if segment is None:
                return _detail(conn, run, current=mapping)
            token = uuid.uuid4().hex
            conn.execute('''UPDATE mapping_extraction_segments SET status='running',attempts=attempts+1,
                            claim_token=?,claimed_at=?,error='' WHERE id=? AND status='pending' ''',
                         (token, time.time(), segment['id']))
            conn.execute("UPDATE mapping_extraction_runs SET status='running' WHERE id=?", (run_id,))
    if mapping is None:
        raise ExtractionConflict(_STALE)
    output, error = None, ''
    try:
        messages = extraction_output.build_messages(mapping=mapping, evidence_text=segment['evidence_text'])
    except Exception:
        # Model/provider/parser details may contain private source material.
        error = _FAILED
    if not error:
        # Prompt construction can outlive the claim or the approved source
        # state. Commit conflicts before raising, outside the provider catch.
        with closing(db.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            run = _row(conn, run_id, workspace_key)
            live = conn.execute('''SELECT 1 FROM mapping_extraction_segments WHERE id=?
                AND status='running' AND claim_token=?''', (segment['id'], token)).fetchone()
            if not live or run['status'] != 'running':
                return _detail(conn, run)
            mapping = _checked_current(conn, run)
        if mapping is None:
            raise ExtractionConflict(_STALE)
        try:
            raw = llm.chat('fast', messages, timeout_seconds=45, max_tokens=4000,
                           should_cancel=lambda: not _claim_is_current(run_id, workspace_key, segment['id'], token))
            output = extraction_output.parse_output(raw, mapping=mapping, evidence_text=segment['evidence_text'])
            serialized = _json(output)
        except Exception:
            error = _FAILED
    with closing(db.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        run = _row(conn, run_id, workspace_key)
        live = conn.execute('''SELECT 1 FROM mapping_extraction_segments WHERE id=?
            AND status='running' AND claim_token=?''', (segment['id'], token)).fetchone()
        if not live or run['status'] != 'running':
            return _detail(conn, run)
        mapping = _checked_current(conn, run)
        if mapping is not None:
            conn.execute('''UPDATE mapping_extraction_segments SET status=?,error=?,output_json=?,
                claim_token=NULL,claimed_at=NULL WHERE id=? AND status='running' AND claim_token=?''',
                         ('failed' if error else 'succeeded', error, None if error else serialized,
                          segment['id'], token))
            _status(conn, run_id)
            result = _detail(conn, _row(conn, run_id, workspace_key), current=mapping)
    if mapping is None:
        raise ExtractionConflict(_STALE)
    return result


def cancel_run(run_id, *, workspace_key):
    """Invalidate all outstanding responses, retaining successful segments."""
    run_id, workspace_key = _id(run_id), _workspace(workspace_key)
    with closing(db.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        run = _row(conn, run_id, workspace_key)
        if run['status'] not in ('completed', 'cancelled', 'conflict'):
            conn.execute('''UPDATE mapping_extraction_segments SET status='cancelled',claim_token=NULL,
                claimed_at=NULL,error='' WHERE run_id=? AND status IN ('pending','running','failed')''', (run_id,))
            conn.execute("UPDATE mapping_extraction_runs SET status='cancelled',error='' WHERE id=?", (run_id,))
        return _detail(conn, _row(conn, run_id, workspace_key))


def retry_run(run_id, *, workspace_key):
    """Explicitly retry unsuccessful work; a live claim expires after 120 seconds."""
    run_id, workspace_key = _id(run_id), _workspace(workspace_key)
    with closing(db.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        run = _row(conn, run_id, workspace_key)
        mapping = _checked_current(conn, run)
        if mapping is not None:
            if run['status'] == 'conflict':
                raise ExtractionConflict(_STALE)
            live = conn.execute('''SELECT claimed_at FROM mapping_extraction_segments
                WHERE run_id=? AND status='running' ''', (run_id,)).fetchone()
            if live and time.time() - live['claimed_at'] < CLAIM_TIMEOUT_SECONDS:
                raise ExtractionConflict('Extraction is still running; retry after the claim expires')
            conn.execute('''UPDATE mapping_extraction_segments SET status='pending',error='',claim_token=NULL,
                claimed_at=NULL WHERE run_id=? AND status IN ('failed','cancelled','running')''', (run_id,))
            _status(conn, run_id)
            result = _detail(conn, _row(conn, run_id, workspace_key), current=mapping)
    if mapping is None:
        raise ExtractionConflict(_STALE)
    return result
