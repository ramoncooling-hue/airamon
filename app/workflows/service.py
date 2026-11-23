"""Persistence and validation for user-authored workflow definitions."""
from __future__ import annotations

import json
import re
import sqlite3
import math
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread, current_thread
from zoneinfo import ZoneInfo
from typing import Any

from .. import runtime
from ..agent import chat as agent_chat
from ..hub import registry
from ..memory import db
from ..memory import sources

KEY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
# A run starts in a background thread, but the create/continue request waits a
# short grace period so instantly-finishing (or approval-only) flows still
# answer with their first meaningful state. Slow steps keep returning 'running'
# and clients poll /api/workflow-runs/{id}.
ASYNC_START_WAIT_SECONDS = 0.5
QUIESCENT_RUN_STATUSES = {'waiting_approval', 'success', 'error', 'cancelled'}
_workers_lock = Lock()
_workers: dict[int, Thread] = {}
SECRET_RE = re.compile(r'(authorization|api[_-]?key|token|password|passwd|secret|cookie|credential)|\b(?:bearer|sk-[A-Za-z0-9_-]+)', re.I)
WORKFLOW_FIELDS = {'key', 'name', 'workspace_key', 'description', 'trigger', 'trigger_json', 'status'}
STEP_FIELDS = {'key', 'kind', 'name', 'agent_key', 'skill_key', 'config', 'input_map', 'output_schema', 'on_error'}


def _key(value: Any, label: str = 'key') -> str:
    if not isinstance(value, str) or not KEY_RE.fullmatch(value):
        raise ValueError(f'{label} 必须是安全标识（字母、数字、下划线或短横线）')
    return value


def _object(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f'{field} 必须是对象')
    return value


def _json(value: str) -> dict:
    try:
        result = json.loads(value or '{}')
    except (TypeError, ValueError) as exc:
        raise ValueError('数据库中的 JSON 字段无效') from exc
    if not isinstance(result, dict):
        raise ValueError('数据库中的 JSON 字段必须是对象')
    return result


def _step(row) -> dict:
    data = dict(row)
    for field in ('config', 'input_map', 'output_schema'):
        data[field] = _json(data.pop(field + '_json'))
    return data


def _workflow(row, conn) -> dict:
    result = dict(row)
    result['trigger'] = _json(result.pop('trigger_json'))
    result['steps'] = [_step(r) for r in conn.execute(
        'SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY position,id', (result['id'],))]
    return result


class WorkflowService:
    # Verbose aliases are kept on the service object for API callers that use
    # the same names as the module-level contract.
    def list_workflows(self, workspace_key=None): return self.list(workspace_key)
    def get_workflow(self, key): return self.get(key)
    def create_workflow(self, body): return self.create(body)
    def update_workflow(self, key, body): return self.update(key, body)
    def delete_workflow(self, key): return self.delete(key)
    def export_workflow(self, key): return self.export_config(key)

    def list(self, workspace_key=None):
        conn = db.connect()
        try:
            sql, params = 'SELECT * FROM workflows', ()
            if workspace_key is not None:
                sql, params = sql + ' WHERE workspace_key=?', (workspace_key,)
            rows = conn.execute(sql + ' ORDER BY updated_at DESC,id DESC', params).fetchall()
            return [_workflow(row, conn) for row in rows]
        finally:
            conn.close()

    def get(self, key):
        key = _key(key, 'workflow key')
        conn = db.connect()
        try:
            row = conn.execute('SELECT * FROM workflows WHERE key=?', (key,)).fetchone()
            if row is None:
                raise ValueError(f'工作流不存在：{key}')
            return _workflow(row, conn)
        finally:
            conn.close()

    def _get_by_id(self, workflow_id):
        conn = db.connect()
        try:
            row = conn.execute('SELECT * FROM workflows WHERE id=?', (workflow_id,)).fetchone()
            if row is None:
                raise ValueError(f'工作流不存在：{workflow_id}')
            return _workflow(row, conn)
        finally:
            conn.close()

    def create(self, body):
        body = _object(body, '工作流')
        _unknown(body, WORKFLOW_FIELDS)
        key = _key(body.get('key', ''))
        name = _text(body.get('name'), 'name', required=True)
        workspace = _key(body.get('workspace_key', 'studypal'), 'workspace_key')
        desc = _text(body.get('description', ''), 'description')
        trigger_value = body.get('trigger_json', body.get('trigger', {}))
        trigger = _object(trigger_value, 'trigger_json' if 'trigger_json' in body else 'trigger')
        status = body.get('status', 'draft')
        if not isinstance(status, str) or status not in {'draft', 'enabled', 'disabled'}:
            raise ValueError('status 只能是 draft、enabled 或 disabled')
        conn = db.connect()
        try:
            cur = conn.execute('INSERT INTO workflows(key,name,workspace_key,description,trigger_json,status) VALUES(?,?,?,?,?,?)',
                               (key, name, workspace, desc, _dump(trigger), status))
            conn.commit()
            return self._get_by_id(cur.lastrowid)
        except Exception as exc:
            conn.rollback()
            raise _translate_db_error(exc)
        finally:
            conn.close()

    def update(self, key, body):
        body = _object(body, '工作流')
        _unknown(body, WORKFLOW_FIELDS - {'key'})
        existing = self.get(key)
        values = {
            'name': _text(body.get('name', existing['name']), 'name', required=True),
            'workspace_key': _key(body.get('workspace_key', existing['workspace_key']), 'workspace_key'),
            'description': _text(body.get('description', existing['description']), 'description'),
            'trigger_json': _dump(_object(body.get('trigger_json', body.get('trigger', existing['trigger'])), 'trigger_json')),
            'status': body.get('status', existing['status']),
        }
        if not isinstance(values['status'], str) or values['status'] not in {'draft', 'enabled', 'disabled'}:
            raise ValueError('status 只能是 draft、enabled 或 disabled')
        conn = db.connect()
        try:
            conn.execute("UPDATE workflows SET name=?,workspace_key=?,description=?,trigger_json=?,status=?,version=version+1,updated_at=datetime('now','localtime') WHERE id=?",
                         (*values.values(), existing['id']))
            conn.commit()
        finally:
            conn.close()
        return self.get(key)

    def delete(self, key):
        existing = self.get(key)
        conn = db.connect()
        try:
            conn.execute('DELETE FROM workflows WHERE id=?', (existing['id'],))
            conn.commit()
        finally:
            conn.close()
        return True

    def add_step(self, workflow_key, body):
        wf = self.get(workflow_key)
        data = _validate_step(body)
        self._validate_refs(data)
        conn = db.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            position = conn.execute('SELECT COALESCE(MAX(position),-1)+1 FROM workflow_steps WHERE workflow_id=?', (wf['id'],)).fetchone()[0]
            cur = self._insert_step(conn, wf['id'], data, position)
            conn.execute("UPDATE workflows SET version=version+1,updated_at=datetime('now','localtime') WHERE id=?", (wf['id'],))
            conn.commit()
            return self._get_step(conn, wf['id'], data['key'])
        except Exception as exc:
            conn.rollback()
            raise _translate_db_error(exc)
        finally:
            conn.close()

    def update_step(self, workflow_key, step_key, body):
        wf = self.get(workflow_key)
        conn = db.connect()
        try:
            old = self._find_step(conn, wf['id'], step_key)
            data = _validate_step({**{k: old[k] for k in STEP_FIELDS}, **_object(body, '步骤')})
            self._validate_refs(data)
            conn.execute('''UPDATE workflow_steps SET key=?,kind=?,name=?,agent_key=?,skill_key=?,config_json=?,input_map_json=?,output_schema_json=?,on_error=?,updated_at=datetime('now','localtime') WHERE id=?''',
                         (data['key'],data['kind'],data['name'],data['agent_key'],data['skill_key'],_dump(data['config']),_dump(data['input_map']),_dump(data['output_schema']),data['on_error'],old['id']))
            conn.execute("UPDATE workflows SET version=version+1,updated_at=datetime('now','localtime') WHERE id=?", (wf['id'],))
            conn.commit()
            return self._get_step(conn, wf['id'], data['key'])
        except Exception as exc:
            conn.rollback()
            raise _translate_db_error(exc)
        finally:
            conn.close()

    def delete_step(self, workflow_key, step_key):
        wf = self.get(workflow_key)
        conn = db.connect()
        try:
            old = self._find_step(conn, wf['id'], step_key)
            conn.execute('DELETE FROM workflow_steps WHERE id=?', (old['id'],))
            self._reposition(conn, wf['id'])
            self._touch(conn, wf['id'])
            conn.commit()
        finally:
            conn.close()
        return True

    def reorder_steps(self, workflow_key, ordered_keys):
        wf = self.get(workflow_key)
        if (not isinstance(ordered_keys, list)
                or not all(isinstance(key, str) for key in ordered_keys)):
            raise ValueError('ordered_keys 必须是字符串 key 数组')
        conn = db.connect()
        try:
            rows = conn.execute('SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY position', (wf['id'],)).fetchall()
            current_keys = {r['key'] for r in rows}
            if (len(ordered_keys) != len(rows)
                    or len(set(ordered_keys)) != len(rows)
                    or set(ordered_keys) != current_keys):
                raise ValueError('排序必须包含所有步骤且不能重复')
            selected = {r['key']: r for r in rows}
            conn.execute('UPDATE workflow_steps SET position=position+100000 WHERE workflow_id=?', (wf['id'],))
            for pos, key in enumerate(ordered_keys):
                row = selected[key]
                conn.execute('UPDATE workflow_steps SET position=? WHERE id=?', (pos, row['id']))
            self._touch(conn, wf['id'])
            conn.commit()
        finally:
            conn.close()
        return self._get_by_id(wf['id'])['steps']

    def create_from_template(self, template, workflow_id=None):
        if not isinstance(template, str) or not template:
            raise ValueError('template 必须是非空字符串')
        if workflow_id is not None and (type(workflow_id) is not int or workflow_id <= 0):
            raise ValueError('workflow_id 必须是正整数')
        templates = _templates()
        if template not in templates:
            raise ValueError(f'未知工作流模板：{template}')
        steps = templates[template]
        for step in steps:
            self._validate_refs(_validate_step(step))
        conn = db.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            if workflow_id is None:
                cur = conn.execute('INSERT INTO workflows(key,name,trigger_json,status) VALUES(?,?,?,?)',
                                   (template, {'material_intake':'课程材料入库','paper_research':'论文资料整理'}[template], '{}', 'draft'))
                workflow_id = cur.lastrowid
            else:
                row = conn.execute('SELECT * FROM workflows WHERE id=?', (workflow_id,)).fetchone()
                if row is None:
                    raise ValueError(f'工作流不存在：{workflow_id}')
                if conn.execute('SELECT 1 FROM workflow_steps WHERE workflow_id=?', (workflow_id,)).fetchone():
                    raise ValueError('只能为没有步骤的工作流应用模板')
            for position, step in enumerate(steps):
                self._insert_step(conn, workflow_id, _validate_step(step), position)
            conn.execute("UPDATE workflows SET version=version+1,updated_at=datetime('now','localtime') WHERE id=?", (workflow_id,))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise _translate_db_error(exc)
        finally:
            conn.close()
        return self._get_by_id(workflow_id)

    def export_config(self, key):
        wf = self.get(key)
        clean = {'key':_sanitize(wf['key']),'name':_sanitize(wf['name']),'workspace_key':_sanitize(wf['workspace_key']),'description':_sanitize(wf['description']),
                 'trigger':_sanitize(wf['trigger']), 'status':wf['status'], 'version':wf['version'],
                 'steps':[]}
        for s in wf['steps']:
            clean['steps'].append({k:_sanitize(s[k]) for k in ('key','position','kind','name','agent_key','skill_key','config','input_map','output_schema','on_error')})
        return clean

    def run(self, workflow_key, input_data=None, trigger_type='manual', context=None):
        # ``context`` was used by the early draft API; keep it as a safe alias.
        if input_data is None and context is not None:
            input_data = context
        return self.start_run(workflow_key, {} if input_data is None else input_data, trigger_type)

    def approve(self, run_id, decision):
        return self.approve_run(run_id, decision)

    def start_run(self, workflow_key, input_data, trigger_type='manual'):
        return self._execute(self._create_run(workflow_key, input_data, trigger_type))

    def start_run_async(self, workflow_key, input_data, trigger_type='manual', wait_seconds=None):
        """Create a run, execute it in a background thread, return a snapshot.

        The create request never blocks on slow Agent/Skill steps; it only waits
        up to ASYNC_START_WAIT_SECONDS for the run to finish or reach an
        approval gate so callers get a meaningful first status when possible.
        """
        run_id = self._create_run(workflow_key, input_data, trigger_type)
        self._dispatch(run_id)
        return self._wait_for_quiescence(run_id, wait_seconds)

    def _create_run(self, workflow_key, input_data, trigger_type='manual'):
        wf = self.get(workflow_key)
        _key(trigger_type, 'trigger_type')
        self._runnable(wf, trigger_type)
        clean_input = _runtime_input(input_data, wf['steps'])
        with _transaction() as conn:
            cur = conn.execute('INSERT INTO workflow_runs(workflow_id,trigger_type,input_json,started_at) VALUES(?,?,?,?)',
                               (wf['id'], trigger_type, _dump(clean_input), _now()))
            run_id = cur.lastrowid
            for step in wf['steps']:
                conn.execute('INSERT INTO workflow_run_steps(run_id,step_id) VALUES(?,?)', (run_id, step['id']))
        return run_id

    def _runnable(self, wf, trigger_type):
        if wf['status'] == 'disabled' or (wf['status'] == 'draft' and trigger_type != 'manual'):
            raise ValueError('disabled 工作流不能运行；draft 只允许手动运行')
        for step in wf['steps']:
            _runtime_config(step)
            self._validate_refs(step)

    def get_run(self, run_id):
        _run_id(run_id)
        conn = db.connect()
        try:
            row = conn.execute('SELECT * FROM workflow_runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise ValueError('运行不存在')
            result = dict(row)
            for field in ('input', 'context', 'output'):
                result[field] = _json(result.pop(field + '_json'))
            result['steps'] = []
            for row in conn.execute('''SELECT r.*,s.key,s.kind,s.position FROM workflow_run_steps r
                JOIN workflow_steps s ON r.step_id=s.id WHERE r.run_id=? ORDER BY s.position,s.id''', (run_id,)):
                item = dict(row)
                for field in ('input', 'output'):
                    item[field] = _json(item.pop(field + '_json'))
                result['steps'].append(item)
            result['step_statuses'] = [s['status'] for s in result['steps']]
            result['execution_counts'] = {s['key']: s['attempt'] for s in result['steps'] if s['attempt']}
            result['executed_step_keys'] = [s['key'] for s in result['steps'] if s['status'] == 'success']
            result['retried_step_keys'] = [s['key'] for s in result['steps'] if s['attempt'] > 1]
            return result
        finally:
            conn.close()

    def list_runs(self, workflow_key, limit=50):
        wf = self.get(workflow_key)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('limit 必须在 1 到 200 之间')
        conn = db.connect()
        try:
            ids = [r[0] for r in conn.execute('SELECT id FROM workflow_runs WHERE workflow_id=? ORDER BY id DESC LIMIT ?', (wf['id'], limit))]
        finally:
            conn.close()
        return [self.get_run(run_id) for run_id in ids]

    def approve_run(self, run_id, decision):
        confirmed = self._record_approval(run_id, decision)
        return self._execute(run_id) if confirmed else self.get_run(run_id)

    def approve_run_async(self, run_id, decision, wait_seconds=None):
        """Apply an approval and let the background thread continue the run."""
        confirmed = self._record_approval(run_id, decision)
        if not confirmed:
            return self.get_run(run_id)
        self._dispatch(run_id)
        return self._wait_for_quiescence(run_id, wait_seconds)

    def _record_approval(self, run_id, decision) -> bool:
        _run_id(run_id)
        confirmed = decision is True or (isinstance(decision, str) and decision in {'confirmed', 'approved'})
        rejected = decision is False or (isinstance(decision, str) and decision in {'rejected', 'cancelled', 'denied'})
        if not confirmed and not rejected:
            raise ValueError('非法 approval decision')
        saved = self.get_run(run_id)
        if confirmed:
            self._runnable(self._get_by_id(saved['workflow_id']), saved['trigger_type'])
        with _transaction() as conn:
            row = conn.execute('SELECT * FROM workflow_runs WHERE id=?', (run_id,)).fetchone()
            if row['status'] != 'waiting_approval':
                raise ValueError('运行不在审批等待状态')
            if rejected:
                conn.execute("UPDATE workflow_runs SET status='cancelled',finished_at=?,duration_ms=? WHERE id=?", (_now(), _elapsed(row['started_at']), run_id))
            else:
                conn.execute("UPDATE workflow_run_steps SET status='success',output_json=?,finished_at=?,duration_ms=? WHERE run_id=? AND step_id=? AND status='waiting_approval'",
                             (_dump({'approved': True}), _now(), _elapsed(next(s['started_at'] for s in saved['steps'] if s['step_id'] == row['current_step_id'])), run_id, row['current_step_id']))
                conn.execute("UPDATE workflow_runs SET status='running',current_step_id=NULL,error=?,finished_at='' WHERE id=?", (saved.get('error', ''), run_id))
        return confirmed

    def cancel_run(self, run_id):
        _run_id(run_id)
        with _transaction() as conn:
            row = conn.execute('SELECT * FROM workflow_runs WHERE id=?', (run_id,)).fetchone()
            if row is None or row['status'] not in {'running', 'waiting_approval', 'error'}:
                raise ValueError('只能取消未完成的运行')
            conn.execute("UPDATE workflow_runs SET status='cancelled',finished_at=?,duration_ms=? WHERE id=?", (_now(), _elapsed(row['started_at']), run_id))
        return self.get_run(run_id)

    def retry_run(self, run_id):
        self._reset_failed_run(run_id)
        return self._execute(run_id)

    def retry_run_async(self, run_id, wait_seconds=None):
        """Reset a failed run and resume it in a background thread."""
        saved = self._reset_failed_run(run_id)
        self._dispatch(run_id)
        return self._wait_for_quiescence(saved['id'], wait_seconds)

    def _reset_failed_run(self, run_id):
        saved = self.get_run(run_id)
        self._runnable(self._get_by_id(saved['workflow_id']), saved['trigger_type'])
        with _transaction() as conn:
            row = conn.execute('SELECT status FROM workflow_runs WHERE id=?', (run_id,)).fetchone()
            if row['status'] != 'error':
                raise ValueError('只能重试失败的运行')
            failed = conn.execute("SELECT step_id FROM workflow_run_steps WHERE run_id=? AND status='error' ORDER BY id LIMIT 1", (run_id,)).fetchone()
            if failed is None:
                raise ValueError('运行没有失败步骤')
            conn.execute("UPDATE workflow_run_steps SET status='pending',error='' WHERE run_id=? AND step_id=?", (run_id, failed['step_id']))
            conn.execute("UPDATE workflow_runs SET status='running',error='',finished_at='' WHERE id=?", (run_id,))
        return saved

    def _dispatch(self, run_id):
        """Start one background worker per run; stale workers are handed over safely.

        A resumed run (approval/retry) may be dispatched while the previous
        worker is still unwinding from its approval pause, so wait briefly for
        that thread to exit instead of dropping the new execution.
        """
        with _workers_lock:
            existing = _workers.get(run_id)
        if existing is not None and existing.is_alive():
            existing.join(timeout=ASYNC_START_WAIT_SECONDS)
        with _workers_lock:
            worker = Thread(target=self._background_execute, args=(run_id,),
                            daemon=True, name=f'mindock-workflow-{run_id}')
            _workers[run_id] = worker
            worker.start()

    def _background_execute(self, run_id):
        try:
            self._execute(run_id)
        except Exception as exc:
            # A lost worker must not leave the run looking alive forever.
            try:
                with _transaction() as conn:
                    row = conn.execute('SELECT status FROM workflow_runs WHERE id=?', (run_id,)).fetchone()
                    if row is not None and row['status'] == 'running':
                        conn.execute("UPDATE workflow_runs SET status='error',error=?,finished_at=? WHERE id=?",
                                     (_safe_error(exc), _now(), run_id))
            except Exception:
                pass
        finally:
            # Only remove this worker's own registration; a follow-up phase may
            # already own the slot.
            current = current_thread()
            with _workers_lock:
                if _workers.get(run_id) is current:
                    _workers.pop(run_id, None)

    def _wait_for_quiescence(self, run_id, wait_seconds=None):
        """Wait briefly for a terminal/approval state, then let polling continue."""
        seconds = ASYNC_START_WAIT_SECONDS if wait_seconds is None else max(0.0, wait_seconds)
        deadline = time.monotonic() + seconds
        while True:
            run = self.get_run(run_id)
            if run['status'] in QUIESCENT_RUN_STATUSES:
                return run
            if time.monotonic() >= deadline:
                return run
            time.sleep(0.01)

    def _execute(self, run_id):
        saved = self.get_run(run_id)
        wf = self._get_by_id(saved['workflow_id'])
        definitions = {s['id']: s for s in wf['steps']}
        context = saved['context']
        run_error = saved.get('error', '')
        for saved_step in saved['steps']:
            if saved_step['status'] in {'success', 'skipped'}:
                continue
            step = definitions[saved_step['step_id']]
            stop = False
            with _transaction() as conn:
                row = conn.execute('SELECT status FROM workflow_runs WHERE id=?', (run_id,)).fetchone()
                if row['status'] != 'running':
                    stop = True
                elif step['kind'] == 'approval':
                    conn.execute("UPDATE workflow_run_steps SET status='waiting_approval',started_at=? WHERE run_id=? AND step_id=? AND status='pending'", (_now(), run_id, step['id']))
                    conn.execute("UPDATE workflow_runs SET status='waiting_approval',current_step_id=?,duration_ms=? WHERE id=?", (step['id'], _elapsed(saved['started_at']), run_id))
                    stop = True
                else:
                    claimed = conn.execute("UPDATE workflow_run_steps SET status='running',started_at=?,finished_at='' WHERE run_id=? AND step_id=? AND status='pending'", (_now(), run_id, step['id']))
                    stop = claimed.rowcount != 1
                    if not stop:
                        conn.execute('UPDATE workflow_runs SET current_step_id=? WHERE id=?', (step['id'], run_id))
            if stop:
                return self.get_run(run_id)
            # Calls run outside SQLite locks. A concurrent cancel wins at writeback.
            max_attempts = 2 if step['on_error'] == 'retry_once' and saved_step['attempt'] == 0 else 1
            elapsed_ms = saved_step['duration_ms']
            for index in range(max_attempts):
                started = time.monotonic()
                with _transaction() as conn:
                    conn.execute('UPDATE workflow_run_steps SET attempt=attempt+1 WHERE run_id=? AND step_id=?', (run_id, step['id']))
                try:
                    self._validate_refs(step)
                    output = self._perform_step(step, wf, saved['input'], context)
                    output = _project_output(output, step['output_schema'])
                    _check_schema(output, step['output_schema'])
                    _validate_context({**context, step['key']: output})
                    error = ''
                except Exception as exc:
                    output, error = {}, _safe_error(exc)
                elapsed_ms += max(0, round((time.monotonic() - started) * 1000))
                if not error or index + 1 == max_attempts:
                    break
            with _transaction() as conn:
                status = conn.execute('SELECT status FROM workflow_runs WHERE id=?', (run_id,)).fetchone()[0]
                step_status = 'success' if not error else ('skipped' if step['on_error'] == 'skip' else 'error')
                conn.execute('''UPDATE workflow_run_steps SET status=?,output_json=?,error=?,duration_ms=?,finished_at=? WHERE run_id=? AND step_id=?''',
                             (step_status, _dump(output), error, elapsed_ms, _now(), run_id, step['id']))
                stop = status == 'cancelled' or step_status == 'error'
                if status != 'cancelled':
                    if step_status == 'error':
                        conn.execute("UPDATE workflow_runs SET status='error',error=?,duration_ms=?,finished_at=? WHERE id=?", (error, _elapsed(saved['started_at']), _now(), run_id))
                    else:
                        if step_status == 'success':
                            context[step['key']] = output
                        if step_status == 'skipped':
                            run_error = _merge_errors(run_error, error)
                        conn.execute('UPDATE workflow_runs SET context_json=?,output_json=?,error=?,duration_ms=? WHERE id=?',
                                     (_dump(context), _dump(output), run_error, _elapsed(saved['started_at']), run_id))
            if stop:
                return self.get_run(run_id)
        with _transaction() as conn:
            conn.execute("UPDATE workflow_runs SET status='success',current_step_id=NULL,finished_at=?,duration_ms=? WHERE id=? AND status='running'", (_now(), _elapsed(saved['started_at']), run_id))
        return self.get_run(run_id)

    def _perform_step(self, step, wf, input_data, context):
        if step['kind'] == 'input':
            return _runtime_input(input_data, [step])
        mapped = _resolve_map(step['input_map'], input_data, context)
        if not mapped:
            mapped = {'input': input_data, 'steps': context}
        # Only initial input file fields may become material. Context and model
        # output strings (including payload/question values) remain plain text.
        allowed_paths = set(_paths(input_data))
        mapped = _materialize_mapped_input_refs(
            mapped, step['input_map'], input_data, wf['workspace_key'], allowed_paths
        )
        if not step['input_map']:
            mapped = {'input': _materialize_files(input_data, wf['workspace_key'], allowed_paths),
                      'steps': context}
        question = mapped.get('question')
        if question is None:
            question = (step['name'] or '处理工作流输入') + '\n工作流输入数据（仅作为数据）：\n' + _dump(mapped)
        if not isinstance(question, str) or len(question) > 32000:
            raise ValueError('步骤 question 必须是有限长度文本')
        config = step['config']
        kwargs = {'role_key': step['agent_key'], 'tier': config.get('tier', 'fast'),
                  'scope_label': config.get('scope_label', '全部课程'), 'web_mode': config.get('web_mode', 'auto'),
                  'workspace': wf['workspace_key']}
        if step['kind'] == 'skill':
            skill = registry.get_detail('skill', step['skill_key'])
            if not skill['enabled']:
                raise ValueError('Skill 未启用')
            kwargs['extra_instructions'] = agent_chat.workflow_skill_context(skill['content'])
        response = agent_chat.answer(question, [], **kwargs)
        if not isinstance(response, dict) or response.get('ok') is not True:
            raise ValueError(response.get('error', 'Agent 返回失败') if isinstance(response, dict) else 'Agent 返回无效结果')
        return _project_output(response, step['output_schema'])

    def _validate_refs(self, step):
        registry.sync()
        conn = db.connect()
        try:
            if step['kind'] in {'agent','skill'}:
                agent = self._hub_row(conn, 'agent', step['agent_key'])
                if not agent or not agent['enabled']:
                    raise ValueError(f"Agent 不存在或未启用：{step['agent_key']}")
            if step['kind'] == 'skill':
                skill = self._hub_row(conn, 'skill', step['skill_key'])
                if not skill or not skill['enabled']:
                    raise ValueError(f"Skill 不存在或未启用：{step['skill_key']}")
        finally:
            conn.close()

    @staticmethod
    def _find_step(conn, workflow_id, key):
        key = _key(key, 'step key')
        row = conn.execute('SELECT * FROM workflow_steps WHERE workflow_id=? AND key=?', (workflow_id, key)).fetchone()
        if row is None:
            raise ValueError(f'步骤不存在：{key}')
        return _step(row)

    @classmethod
    def _get_step(cls, conn, workflow_id, key):
        return cls._find_step(conn, workflow_id, key)

    @staticmethod
    def _insert_step(conn, workflow_id, data, position):
        try:
            return conn.execute('''INSERT INTO workflow_steps(workflow_id,key,position,kind,name,agent_key,skill_key,config_json,input_map_json,output_schema_json,on_error)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (workflow_id, data['key'], position, data['kind'], data['name'], data['agent_key'], data['skill_key'],
                _dump(data['config']), _dump(data['input_map']), _dump(data['output_schema']), data['on_error']))
        except Exception as exc:
            raise _translate_db_error(exc)

    @staticmethod
    def _hub_row(conn, kind, key):
        table = 'skills' if kind == 'skill' else 'agents'
        row = conn.execute(f'SELECT * FROM {table} WHERE key=?', (key,)).fetchone()
        if not row:
            return None
        base, _, content_name = registry._kind_config(kind)
        directory = base / key
        manifest = directory / 'manifest.json'
        content = directory / content_name
        if not directory.is_dir() or not manifest.is_file() or not content.is_file():
            return None
        try:
            data = registry._manifest(directory, key)
        except (ValueError, OSError):
            return None
        if data.get('key') != key:
            return None
        return row

    @staticmethod
    def _reposition(conn, workflow_id):
        rows = conn.execute('SELECT id FROM workflow_steps WHERE workflow_id=? ORDER BY position,id', (workflow_id,)).fetchall()
        conn.execute('UPDATE workflow_steps SET position=position+100000 WHERE workflow_id=?', (workflow_id,))
        for pos, row in enumerate(rows):
            conn.execute('UPDATE workflow_steps SET position=? WHERE id=?', (pos, row['id']))

    @staticmethod
    def _touch(conn, workflow_id):
        conn.execute("UPDATE workflows SET version=version+1,updated_at=datetime('now','localtime') WHERE id=?", (workflow_id,))


def _unknown(body, allowed):
    unknown = set(body) - allowed
    if unknown:
        raise ValueError(f'未知字段：{sorted(unknown)[0]}')


@contextmanager
def _transaction():
    conn = db.connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='milliseconds')


def _elapsed(start):
    return max(0, round((datetime.now(ZoneInfo('Asia/Shanghai')) - datetime.fromisoformat(start)).total_seconds() * 1000))


def _run_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError('run_id 必须是正整数')


def _validate_context(value, depth=0):
    if depth > 12:
        raise ValueError('上下文嵌套过深')
    if isinstance(value, dict):
        if len(value) > 100 or not all(isinstance(k, str) and len(k) <= 100 for k in value):
            raise ValueError('上下文字段无效或过多')
        for item in value.values():
            _validate_context(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 100:
            raise ValueError('上下文数组过长')
        for item in value:
            _validate_context(item, depth + 1)
    elif isinstance(value, str):
        if len(value) > 32000:
            raise ValueError('上下文文本过长')
    elif value is None or type(value) in {bool, int}:
        pass
    elif type(value) is float and math.isfinite(value):
        pass
    else:
        raise ValueError('上下文必须是有限 JSON 数据')
    if depth == 0 and len(_dump(value).encode('utf-8')) > 512000:
        raise ValueError('上下文超过大小限制')


def _safe_path(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('文件路径无效')
    root = runtime.data_dir().resolve()
    path = Path(value)
    path = (path if path.is_absolute() else root / path).resolve()
    if path == root or root not in path.parents:
        raise ValueError('文件路径越出 runtime data root')
    if not path.is_file():
        raise ValueError('输入文件不存在')
    # Settings and Hub files can contain credentials or executable guidance.
    if any(p.lower() in {'app', 'settings', '.git'} for p in path.relative_to(root).parts) or path.name.startswith('.env'):
        raise ValueError('不能将运行配置作为文件输入')
    return str(path)


def _paths(value, discover=False):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {'file_path', 'path', 'filename', 'file', 'files', 'file_paths'}:
                items = item if isinstance(item, list) else [item]
                found.extend(_safe_path(p) for p in items)
            else:
                found.extend(_paths(item, discover=discover))
    elif isinstance(value, list):
        for item in value:
            found.extend(_paths(item, discover=discover))
    return found


def _validate_paths(value, allowed=None):
    paths = _paths(value, discover=True)
    if allowed is not None and not set(paths) <= set(_paths(allowed, discover=True)):
        raise ValueError('步骤只能引用输入提供的安全文件')


DEFAULT_INPUT_FIELDS = {'question', 'topic', 'requirements', 'course', 'course_id', 'source_id',
                        'file_path', 'file_paths', 'filename', 'file', 'files', 'path'}


def _runtime_input(value, steps):
    _object(value, 'input_data')
    _validate_context(value)
    _validate_paths(value)
    fields = set()
    inputs = [s for s in steps if s['kind'] == 'input']
    for step in inputs:
        fields.update(step['config'].get('fields', DEFAULT_INPUT_FIELDS))
    if not inputs:
        fields = DEFAULT_INPUT_FIELDS
    return _sanitize({k: v for k, v in value.items() if k in fields})


def _validate_schema(schema, depth=0, root=True):
    _object(schema, 'output_schema')
    if not schema:
        return
    if depth > 8:
        raise ValueError('schema 嵌套过深')
    if root and schema.get('type', 'object') != 'object':
        raise ValueError('顶层 output_schema 必须是 object')
    _unknown(schema, {'type', 'properties', 'required', 'additionalProperties', 'items', 'enum', 'maxLength', 'maxItems'})
    if schema.get('type', 'object') not in {'object', 'array', 'string', 'boolean', 'integer', 'number', 'null'}:
        raise ValueError('schema type 无效')
    properties = _object(schema.get('properties', {}), 'properties')
    for key, child in properties.items():
        _key(key, 'schema field')
        _validate_schema(child, depth + 1, root=False)
    required = schema.get('required', [])
    if not isinstance(required, list) or not all(isinstance(k, str) for k in required):
        raise ValueError('required 必须为字段数组')
    if 'additionalProperties' in schema and type(schema['additionalProperties']) is not bool:
        raise ValueError('additionalProperties 必须为布尔值')
    if 'items' in schema:
        _validate_schema(schema['items'], depth + 1, root=False)
    if 'enum' in schema and (not isinstance(schema['enum'], list) or not schema['enum']):
        raise ValueError('enum 必须是非空数组')
    for field in ('maxLength', 'maxItems'):
        if field in schema and (type(schema[field]) is not int or schema[field] < 0):
            raise ValueError(f'{field} 必须是非负整数')


def _check_schema(value, schema):
    if not schema:
        return
    kind = schema.get('type', 'object')
    valid = {'object': isinstance(value, dict), 'array': isinstance(value, list), 'string': isinstance(value, str),
             'boolean': type(value) is bool, 'integer': type(value) is int,
             'number': type(value) in {int, float}, 'null': value is None}[kind]
    if not valid or ('enum' in schema and value not in schema['enum']):
        raise ValueError('输出不符合 schema')
    if kind == 'object':
        if set(schema.get('required', [])) - set(value):
            raise ValueError('输出缺少 required 字段')
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is False and set(value) - set(properties):
            raise ValueError('输出包含未知字段')
        for key in set(value) & set(properties):
            _check_schema(value[key], properties[key])
    if kind == 'array':
        if len(value) > schema.get('maxItems', 100):
            raise ValueError('输出数组过长')
        for item in value:
            _check_schema(item, schema.get('items', {}))
    if kind == 'string' and len(value) > schema.get('maxLength', 32000):
        raise ValueError('输出文本过长')


def _runtime_config(step):
    config = step['config']
    if step['kind'] in {'agent', 'skill'}:
        _unknown(config, {'tier', 'scope_label', 'web_mode'})
        if config.get('tier', 'fast') not in {'fast', 'reasoning'}:
            raise ValueError('tier 必须是 fast 或 reasoning')
        if config.get('web_mode', 'auto') not in {'auto', 'on', 'off'}:
            raise ValueError('web_mode 无效')
        if not isinstance(config.get('scope_label', ''), str) or len(config.get('scope_label', '')) > 200:
            raise ValueError('scope_label 无效')
    if step['kind'] == 'input':
        fields = config.get('fields', [])
        if not isinstance(fields, list) or not all(isinstance(f, str) and KEY_RE.fullmatch(f) and not SECRET_RE.search(f) for f in fields):
            raise ValueError('input fields 无效')
        if config.get('accept', 'text') not in {'text', 'file'}:
            raise ValueError('input accept 无效')
    for key, ref in step['input_map'].items():
        _key(key, 'input_map field')
        if SECRET_RE.search(key) or not isinstance(ref, str) or not re.fullmatch(r'(?:\$\.)?[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*', ref):
            raise ValueError('input_map 只能包含安全 context 字段引用')
    _validate_schema(step['output_schema'])


def _resolve_map(mapping, input_data, context):
    roots = {**context, 'steps': context, 'input': input_data, 'context': context}
    result = {}
    for key, ref in mapping.items():
        value = roots
        for part in ref.removeprefix('$.').split('.'):
            if not isinstance(value, dict) or part not in value:
                raise ValueError('input_map 引用了不存在的字段')
            value = value[part]
        result[key] = value
    return result


def _summary(response):
    result = {'ok': True, 'text': _sanitize(str(response.get('text', '')))[:8000]}
    for field, allowed in (
        ('hits', {'id', 'kind', 'name', 'title', 'citation'}),
        ('web_results', {'title', 'url', 'citation'}),
        ('trace', {'type', 'tool', 'status', 'count'}),
    ):
        rows = response.get(field, [])
        if not isinstance(rows, list):
            rows = []
        result[field] = [{k: (_sanitize(v)[:500] if isinstance(v, str) else v)
                          for k, v in row.items() if k in allowed and (v is None or type(v) in {str, int, bool})}
                         for row in rows[:8] if isinstance(row, dict)]
    return result


def _bounded_value(value, depth=0):
    """Keep custom schema output useful without persisting model payloads."""
    if depth > 8:
        return '[TRUNCATED]'
    if isinstance(value, dict):
        return {str(k): _bounded_value(v, depth + 1) for k, v in list(value.items())[:50]
                if not SECRET_RE.search(str(k))}
    if isinstance(value, list):
        return [_bounded_value(v, depth + 1) for v in value[:20]]
    if isinstance(value, str):
        return _sanitize(value)[:8000]
    if value is None or type(value) in {bool, int, float}:
        return value
    return str(value)[:500]


def _project_output(response, schema):
    """Project Agent output to the workflow's declared schema before persistence."""
    if not schema:
        return _summary(response)
    if schema.get('type', 'object') == 'object':
        properties = schema.get('properties', {})
        return {key: _bounded_value(response[key]) for key in properties if key in response}
    return _bounded_value(response)


def _merge_errors(previous, current):
    if not current:
        return previous
    if not previous:
        return current[:500]
    if current in previous:
        return previous[:500]
    return (previous + '; ' + current)[:500]


FILE_FIELDS = {'file_path', 'path', 'filename', 'file', 'files', 'file_paths'}


def _materialize_mapped_input_refs(value, mapping, input_data, workspace, allowed_paths):
    """Materialize only input_map references rooted at the initial input."""
    if not isinstance(mapping, dict):
        return value
    result = dict(value)
    for output_key, ref in mapping.items():
        if not isinstance(ref, str):
            continue
        parts = ref.removeprefix('$.').split('.')
        if len(parts) < 2 or parts[0] != 'input' or parts[-1] not in FILE_FIELDS:
            continue
        source = input_data
        try:
            for part in parts[1:]:
                source = source[part]
        except (KeyError, TypeError):
            raise ValueError('input_map 引用了不存在的初始文件字段')
        if isinstance(source, list):
            contexts = []
            for item in source:
                canonical = _safe_path(item)
                if canonical not in allowed_paths:
                    raise ValueError('文件引用必须来自初始 input_data')
                contexts.append(_file_context(item, workspace))
            result[output_key] = contexts
        else:
            canonical = _safe_path(source)
            if canonical not in allowed_paths:
                raise ValueError('文件引用必须来自初始 input_data')
            material = _file_context(source, workspace)
            if output_key == 'question':
                if material.get('kind') == 'text_material':
                    result[output_key] = f"材料：{material['name']}\n{material['content']}"
                else:
                    result[output_key] = f"材料索引：{json.dumps(material, ensure_ascii=False)}"
            else:
                result[output_key] = material
    return result


def _materialize_files(value, workspace, allowed_paths):
    """Replace safe file paths in Agent input with bounded text/index context."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in FILE_FIELDS and isinstance(item, str):
                canonical = _safe_path(item)
                if canonical not in allowed_paths:
                    raise ValueError('文件引用必须来自初始 input_data')
                result[key] = _file_context(item, workspace)
            elif key in {'files', 'file_paths'} and isinstance(item, list):
                result[key] = [_materialize_files(path, workspace, allowed_paths) if isinstance(path, dict)
                               else (_file_context(path, workspace) if _safe_path(path) in allowed_paths else
                                     (_raise_file_provenance()))
                               for path in item]
            else:
                result[key] = _materialize_files(item, workspace, allowed_paths)
        return result
    if isinstance(value, list):
        return [_materialize_files(item, workspace, allowed_paths) for item in value]
    return value


def _raise_file_provenance():
    raise ValueError('文件引用必须来自初始 input_data')


def _file_context(raw_path, workspace):
    path = Path(_safe_path(raw_path))
    if path.suffix.lower() in {'.txt', '.md', '.markdown', '.csv'}:
        try:
            text = path.read_text(encoding='utf-8-sig', errors='replace')[:6000]
            return {'name': path.name, 'kind': 'text_material', 'content': text}
        except OSError as exc:
            raise ValueError('无法读取输入材料') from exc
    # Binary formats are represented by existing indexed chunks, never raw bytes.
    indexed = sources.search(path.name, limit=4, workspace=workspace)
    return {'name': path.name, 'kind': 'indexed_material',
            'summaries': [{'name': row.get('name', ''), 'gist': str(row.get('gist', ''))[:1200],
                           'source': row.get('source', '')} for row in indexed]}


def _safe_error(exc):
    # Network exceptions may include request headers, query credentials or bodies.
    message = str(exc)
    if SECRET_RE.search(message):
        return '步骤执行失败（敏感错误详情已隐藏）'
    return message[:500] or '步骤执行失败'


def _translate_db_error(exc):
    if isinstance(exc, sqlite3.IntegrityError):
        message = str(exc)
        if 'workflows.key' in message or 'UNIQUE constraint failed: workflows' in message:
            return ValueError('工作流 key 已存在')
        if 'workflow_steps' in message:
            return ValueError('步骤 key 或 position 已存在')
        return ValueError('工作流数据冲突')
    return exc


def _text(value, field, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f'{field} 必须是非空文本' if required else f'{field} 必须是文本')
    return value


def _dump(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        raise ValueError('JSON 字段必须是可序列化的对象') from exc


def _validate_step(body):
    body = _object(body, '步骤')
    _unknown(body, STEP_FIELDS)
    data = {'key':_key(body.get('key',''), 'step key'), 'kind':body.get('kind'), 'name':_text(body.get('name',''), 'name'),
            'agent_key':body.get('agent_key',''), 'skill_key':body.get('skill_key',''), 'on_error':body.get('on_error','pause')}
    if not isinstance(data['kind'], str) or data['kind'] not in {'input','agent','skill','approval'}:
        raise ValueError('step kind 只能是 input、agent、skill 或 approval')
    if not isinstance(data['on_error'], str) or data['on_error'] not in {'pause','retry_once','skip'}:
        raise ValueError('on_error 只能是 pause、retry_once 或 skip')
    for field in ('agent_key','skill_key'):
        if not isinstance(data[field], str):
            raise ValueError(f'{field} 必须是字符串')
        if data[field]:
            _key(data[field], field)
    if data['kind'] == 'input' and (data['agent_key'] or data['skill_key']):
        raise ValueError('input 步骤不能引用 Agent 或 Skill')
    if data['kind'] == 'agent' and (not data['agent_key'] or data['skill_key']):
        raise ValueError('agent 步骤必须只引用 agent_key')
    if data['kind'] == 'skill' and (not data['skill_key'] or not data['agent_key']):
        raise ValueError('skill 步骤必须引用 skill_key 和 agent_key')
    if data['kind'] == 'approval' and (data['agent_key'] or data['skill_key']):
        raise ValueError('approval 步骤不能引用 Agent 或 Skill')
    for field in ('config','input_map','output_schema'):
        data[field] = _object(body.get(field, {}), field)
    # Validate executable mappings and schemas when saving as well as at run time.
    for key, ref in data['input_map'].items():
        _key(key, 'input_map field')
        if SECRET_RE.search(key) or not isinstance(ref, str) or not re.fullmatch(r'(?:\$\.)?[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*', ref):
            raise ValueError('input_map 只能包含安全 context 字段引用')
    _validate_schema(data['output_schema'])
    return data


def _templates():
    return {
      'material_intake': [
        {'key':'upload','kind':'input','name':'上传文件','config':{'accept':'file'}},
        {'key':'recognize','kind':'skill','name':'识别材料','skill_key':'material-recognition','agent_key':'laoba'},
        {'key':'wiki','kind':'skill','name':'Wiki 拆录','skill_key':'wiki-extract','agent_key':'laoba'},
        {'key':'review','kind':'approval','name':'确认候选'},
        {'key':'write_graph','kind':'agent','name':'写入图谱与掌握度','agent_key':'laoba'},
      ],
      'paper_research': [
        {'key':'topic','kind':'input','name':'输入课题和要求'},
        {'key':'web_research','kind':'skill','name':'互联网检索','skill_key':'web-research','agent_key':'laoba'},
        {'key':'local_research','kind':'skill','name':'本地检索','skill_key':'local-research','agent_key':'laoba'},
        {'key':'select_evidence','kind':'approval','name':'选择论据'},
        {'key':'draft','kind':'agent','name':'生成论文初稿','agent_key':'paper'},
        {'key':'final_review','kind':'approval','name':'最终确认'},
      ]}


def _sanitize(value):
    if isinstance(value, dict):
        return {k:_sanitize(v) for k,v in value.items() if not SECRET_RE.search(str(k)) and str(k).lower() not in {'file_content','raw_content','run_context','context'}}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str) and SECRET_RE.search(value):
        return '[REDACTED]'
    return value


def list_workflows(workspace_key=None): return WorkflowService().list(workspace_key)
def get_workflow(key): return WorkflowService().get(key)
def create_workflow(body): return WorkflowService().create(body)
def update_workflow(key, body): return WorkflowService().update(key, body)
def delete_workflow(key): return WorkflowService().delete(key)
def add_step(workflow_key, body): return WorkflowService().add_step(workflow_key, body)
def update_step(workflow_key, step_key, body): return WorkflowService().update_step(workflow_key, step_key, body)
def delete_step(workflow_key, step_key): return WorkflowService().delete_step(workflow_key, step_key)
def reorder_steps(workflow_key, ordered_keys): return WorkflowService().reorder_steps(workflow_key, ordered_keys)
def export_workflow(key): return WorkflowService().export_config(key)
def start_run(workflow_key, input_data, trigger_type='manual'): return WorkflowService().start_run(workflow_key, input_data, trigger_type)
def approve_run(run_id, decision): return WorkflowService().approve_run(run_id, decision)
def cancel_run(run_id): return WorkflowService().cancel_run(run_id)
def retry_run(run_id): return WorkflowService().retry_run(run_id)
def get_run(run_id): return WorkflowService().get_run(run_id)
def list_runs(workflow_key, limit=50): return WorkflowService().list_runs(workflow_key, limit)
def run(workflow_key, input_data=None, trigger_type='manual', context=None):
    if input_data is None and context is not None:
        input_data = context
    return start_run(workflow_key, {} if input_data is None else input_data, trigger_type)
def approve(run_id, decision): return approve_run(run_id, decision)
