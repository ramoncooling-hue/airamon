"""CyberPal · 接口层。

接口按「记忆的生命周期」组织，不按业务模块切：
    /api/*            记忆的读取与写回
    /api/settings/*   记忆的规则（角色、模型、通道）
    /api/agent/*      智能体问答（阶段 3、4）
    /api/intake/*     投放（阶段 5）
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import platform
import re
import sqlite3
import json
import queue
import subprocess
import threading
import io
import zipfile
import time
import html
import uuid
import hashlib
from typing import Any, Literal
from fastapi.responses import Response
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from ..agent import chat as agent_chat
from ..agent import llm
from ..agent import web_search
from ..agent_runtime.actions import ActionService
from ..agent_runtime.context import ContextCompiler
from ..agent_runtime.service import PersonalAgentService
from ..hub import config_composer, registry
from ..memory import (chat_repo, db, repo, seed, sources, graph_tools, calendar_sync,
                      search_index, platform_manual, domain_profiles, relation_suggestions,
                      documents, wikilinks, review as review_queue, wiki_import)
from ..memory import learning
from ..memory import asr, lecture
from ..settings import store as settings_store
from ..workflows import service as workflow_service
from ..ingest import jobs as ingest_jobs
from ..maintenance import diagnostics as diagnostics_service

router = APIRouter(prefix="/api")

from .mcp_routes import router as mcp_router
router.include_router(mcp_router)
from .model_routes import router as model_router
router.include_router(model_router)
from .ontology_routes import router as ontology_router
router.include_router(ontology_router)


# 原始材料先按课程落盘，后续抽取管线只需要消费这个稳定目录。
STUDYPAL_DIR = db.DATA_DIR / "StudyPal"


class IntakeIn(BaseModel):
    course: str
    workspace_key: str = 'studypal'
    filename: str
    content_base64: str
    mime: str = "application/octet-stream"
    source_kind: str = 'unclassified'
    source_role: str = ''


class RevealIn(BaseModel):
    path: str


class CourseIn(BaseModel):
    name: str
    workspace_key: str = 'studypal'
    teacher: str = ""
    nature: str = "选修课"
    credits: float = 0
    note: str = ""


class WebSaveIn(BaseModel):
    course: str
    workspace_key: str = 'studypal'
    title: str
    url: str
    snippet: str = ""
    body: str = ""
    retrieved_at: str = ""


class EventIn(BaseModel):
    record_id: str = ''
    course_id: int | None = None
    course_name: str = ''
    title: str
    category: str = 'personal'
    type_raw: str = ''
    date: str
    time_range: str = ''
    location: str = ''
    note: str = ''
    workspace_key: str = 'studypal'


class WorkspaceIn(BaseModel):
    key: str
    name: str
    kind: str = 'custom'
    domain: str = ''
    description: str = ''


class WorkspaceItemIn(BaseModel):
    workspace: str
    kind: str = 'note'
    title: str
    body: str = ''
    status: str = 'open'
    due_at: str = ''


class ConfigAssistantPreviewIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: Literal['skill', 'mcp']
    prompt: str = Field(min_length=1, max_length=4000)


class AgentProfileIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    display_name: str | None = None
    memory_policy: str | None = None
    preferences: dict[str, Any] | None = None
    active_workspace_key: str | None = None


class AgentGoalIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workspace_key: str = 'studypal'
    title: str
    description: str = ''
    priority: int = 3
    due_at: str = ''
    next_action: str = ''
    source_type: str = ''
    source_id: str = ''


class MemoryBlockIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workspace_key: str = 'studypal'
    layer: str
    title: str
    content: str = ''
    status: str = 'active'
    source_type: str = 'manual'
    source_id: str = ''
    confidence: float = 0.8
    confirmed: bool = True


class AgentRunIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workspace_key: str = 'studypal'
    agent_key: str = 'laoba'
    trigger_type: str = 'manual'
    trigger_id: str = ''
    input: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str = ''


class _WorkflowModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class WorkflowCreateIn(_WorkflowModel):
    key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    workspace_key: str = Field(default='studypal', min_length=1, max_length=64)
    description: str = Field(default='', max_length=2000)
    trigger: dict[str, Any] = Field(default_factory=dict, max_length=100)
    trigger_json: dict[str, Any] | None = Field(default=None, max_length=100)
    status: Literal['draft', 'enabled', 'disabled'] = 'draft'
    template: Literal['material_intake', 'paper_research'] | None = None


class WorkflowUpdateIn(_WorkflowModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    workspace_key: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    trigger: dict[str, Any] | None = Field(default=None, max_length=100)
    trigger_json: dict[str, Any] | None = Field(default=None, max_length=100)
    status: Literal['draft', 'enabled', 'disabled'] | None = None


class WorkflowStepCreateIn(_WorkflowModel):
    key: str = Field(min_length=1, max_length=64)
    kind: Literal['input', 'agent', 'skill', 'approval']
    name: str = Field(default='', max_length=200)
    agent_key: str = Field(default='', max_length=64)
    skill_key: str = Field(default='', max_length=64)
    config: dict[str, Any] = Field(default_factory=dict, max_length=100)
    input_map: dict[str, str] = Field(default_factory=dict, max_length=100)
    output_schema: dict[str, Any] = Field(default_factory=dict, max_length=100)
    on_error: Literal['pause', 'retry_once', 'skip'] = 'pause'


class WorkflowStepUpdateIn(_WorkflowModel):
    key: str | None = Field(default=None, min_length=1, max_length=64)
    kind: Literal['input', 'agent', 'skill', 'approval'] | None = None
    name: str | None = Field(default=None, max_length=200)
    agent_key: str | None = Field(default=None, max_length=64)
    skill_key: str | None = Field(default=None, max_length=64)
    config: dict[str, Any] | None = Field(default=None, max_length=100)
    input_map: dict[str, str] | None = Field(default=None, max_length=100)
    output_schema: dict[str, Any] | None = Field(default=None, max_length=100)
    on_error: Literal['pause', 'retry_once', 'skip'] | None = None


class WorkflowReorderIn(_WorkflowModel):
    ordered_keys: list[str] = Field(min_length=1, max_length=200)


class WorkflowRunIn(_WorkflowModel):
    input_data: dict[str, Any] = Field(default_factory=dict, max_length=100)
    trigger_type: str = Field(default='manual', min_length=1, max_length=64)


class WorkflowApprovalIn(_WorkflowModel):
    decision: StrictBool | Literal['confirmed', 'approved', 'rejected', 'cancelled', 'denied']


class WorkflowImportStep(WorkflowStepCreateIn):
    position: int | None = Field(default=None, ge=0, le=10000)


class WorkflowImportIn(_WorkflowModel):
    key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    workspace_key: str = Field(default='studypal', min_length=1, max_length=64)
    description: str = Field(default='', max_length=2000)
    trigger: dict[str, Any] = Field(default_factory=dict, max_length=100)
    status: Literal['draft', 'enabled', 'disabled'] = 'draft'
    version: int | None = Field(default=None, ge=1)
    steps: list[WorkflowImportStep] = Field(default_factory=list, max_length=200)


def _safe_filename(value: str) -> str:
    name = Path(value or "材料").name.strip() or "材料"
    name = re.sub(r"[^\w.\-（）()\u4e00-\u9fff ]+", "_", name).strip(" .")
    return name[:180] or "材料"


def _workspace_folder(workspace_key: str = 'studypal') -> Path:
    return STUDYPAL_DIR if workspace_key == 'studypal' else db.DATA_DIR / _safe_filename(workspace_key)


def _course_workspace_key(course: str) -> str:
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT COALESCE(w.key,'studypal') AS workspace_key FROM courses c
               LEFT JOIN workspaces w ON w.id=c.workspace_id WHERE c.name=?""", (course,)
        ).fetchone()
    finally:
        conn.close()
    return row['workspace_key'] if row else 'studypal'


def _require_course_scope(course: str, workspace: str | None = None) -> None:
    if course not in {item['name'] for item in repo.list_courses(workspace)}:
        raise HTTPException(404, '当前工作区没有这个知识容器')


def _runtime_path(value: str) -> Path:
    """Resolve a user-visible path and keep OS actions inside the data root."""
    return Path(value or '').expanduser().resolve()


def _inside_runtime_root(path: Path) -> bool:
    root = db.DATA_DIR.expanduser().resolve()
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return path == root


_WORKFLOW_SECRET_FIELD_RE = re.compile(
    r'(api[_-]?key|password|passwd|cookie|token|secret|credential)', re.IGNORECASE
)
_WORKFLOW_PRIVATE_OUTPUT_RE = re.compile(
    r'(prompt|model|model[_-]?name|llm|provider|temperature|top[_-]?p|max[_-]?tokens|'
    r'api[_-]?key|password|passwd|cookie|token|secret|credential|raw[_-]?context)',
    re.IGNORECASE,
)


def _reject_workflow_secrets(value: Any, path: str = 'workflow') -> None:
    """Reject credential-shaped fields before they reach workflow storage."""
    if isinstance(value, dict):
        for key, item in value.items():
            if _WORKFLOW_SECRET_FIELD_RE.search(str(key)):
                raise ValueError(f'{path}.{key} 不允许包含敏感字段')
            _reject_workflow_secrets(item, f'{path}.{key}')
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_workflow_secrets(item, f'{path}[{index}]')


def _scrub_workflow_output(value: Any) -> Any:
    """Keep API responses useful while omitting prompts, model settings and credentials."""
    if isinstance(value, dict):
        return {
            key: _scrub_workflow_output(item)
            for key, item in value.items()
            if not _WORKFLOW_PRIVATE_OUTPUT_RE.search(str(key))
        }
    if isinstance(value, list):
        return [_scrub_workflow_output(item) for item in value]
    return value


def _public_workflow(workflow: dict) -> dict:
    result = {
        key: workflow.get(key)
        for key in ('id', 'key', 'name', 'workspace_key', 'description', 'status', 'version',
                    'created_at', 'updated_at')
        if key in workflow
    }
    result['trigger'] = _scrub_workflow_output(workflow.get('trigger', {}))
    result['steps'] = []
    for step in workflow.get('steps', []):
        result['steps'].append(_scrub_workflow_output({
            key: step.get(key)
            for key in ('id', 'key', 'position', 'kind', 'name', 'agent_key', 'skill_key',
                        'config', 'input_map', 'output_schema', 'on_error', 'created_at', 'updated_at')
            if key in step
        }))
    return result


def _public_run(run: dict) -> dict:
    """Expose execution state without raw input/context/model output payloads."""
    result = {
        key: run.get(key)
        for key in ('id', 'workflow_id', 'trigger_type', 'current_step_id', 'duration_ms',
                    'status', 'started_at', 'finished_at')
        if key in run
    }
    result['steps'] = []
    for step in run.get('steps', []):
        result['steps'].append({
            key: step.get(key)
            for key in ('id', 'step_id', 'key', 'kind', 'name', 'position', 'status', 'attempt',
                        'duration_ms', 'started_at', 'finished_at')
            if key in step
        })
    result['step_statuses'] = [step.get('status') for step in run.get('steps', [])]
    result['execution_counts'] = dict(run.get('execution_counts', {}))
    result['executed_step_keys'] = list(run.get('executed_step_keys', []))
    result['retried_step_keys'] = list(run.get('retried_step_keys', []))
    # Background runs are polled: expose the current step, completion progress
    # and a retryable error message without raw context/model payloads.
    current_step_id = run.get('current_step_id')
    current = next((step for step in result['steps'] if step.get('step_id') == current_step_id), None)
    result['current_step'] = ({'id': current.get('id'), 'key': current.get('key'),
                               'name': current.get('name') or current.get('key')}
                              if current is not None else None)
    total = len(result['steps'])
    completed = sum(1 for step in result['steps'] if step.get('status') in {'success', 'skipped'})
    result['progress'] = {'completed': completed, 'total': total,
                          'percent': round(completed * 100 / total) if total else 0}
    result['error'] = str(run.get('error') or '')[:500]
    return result


def _workflow_http_error(exc: Exception, *, not_found: bool = False) -> HTTPException:
    message = str(exc) or '工作流请求无效'
    missing_resource = any(token in message for token in ('工作流不存在', '步骤不存在', '运行不存在'))
    status = 404 if not_found or missing_resource else 400
    return HTTPException(status, message)


def _workflow_call(function, *args, not_found: bool = False, **kwargs):
    try:
        return function(*args, **kwargs)
    except HTTPException:
        raise
    except ValueError as exc:
        raise _workflow_http_error(exc, not_found=not_found) from exc
    except (sqlite3.Error, TypeError) as exc:
        raise HTTPException(400, '工作流请求无效') from exc


def _validate_workflow_dependencies(steps: list[dict]) -> None:
    """Reject unknown or disabled Agent/Skill refs before importing a definition."""
    registry.sync()
    for step in steps:
        if step.get('kind') not in {'agent', 'skill'}:
            continue
        agent_key = step.get('agent_key', '')
        try:
            agent = registry.get_detail('agent', agent_key)
        except ValueError as exc:
            raise ValueError(f'Agent 不存在或未启用：{agent_key}') from exc
        if not agent.get('enabled'):
            raise ValueError(f'Agent 不存在或未启用：{agent_key}')
        if step.get('kind') == 'skill':
            skill_key = step.get('skill_key', '')
            try:
                skill = registry.get_detail('skill', skill_key)
            except ValueError as exc:
                raise ValueError(f'Skill 不存在或未启用：{skill_key}') from exc
            if not skill.get('enabled'):
                raise ValueError(f'Skill 不存在或未启用：{skill_key}')


def _intake_folder(course: str) -> tuple[str, Path]:
    """Validate a course and return its managed inbox directory."""
    course = (course or '').strip()
    allowed = {c['name'] for c in repo.list_courses()}
    if not course or course not in allowed:
        raise HTTPException(400, '请选择有效课程')
    workspace_key = _course_workspace_key(course)
    folder = _workspace_folder(workspace_key) / _safe_filename(course) / 'inbox'
    folder.mkdir(parents=True, exist_ok=True)
    return workspace_key, folder


def _next_intake_target(folder: Path, filename: str) -> Path:
    """Choose a collision-safe final path without overwriting prior material."""
    safe_name = _safe_filename(filename)
    target = folder / safe_name
    if not target.exists():
        return target
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
    target = folder / f'{stem}-{stamp}{suffix}'
    index = 2
    while target.exists():
        target = folder / f'{stem}-{stamp}-{index}{suffix}'
        index += 1
    return target


def _write_upload_chunk(output, digest, chunk):
    output.write(chunk)
    digest.update(chunk)


def _publish_upload(output, temp_path, folder, filename):
    output.flush()
    os.fsync(output.fileno())
    # Link is atomic and refuses a collision with another concurrent upload.
    while True:
        target = _next_intake_target(folder, filename)
        try:
            os.link(temp_path, target)
            return target
        except FileExistsError:
            continue


def _publish_and_register_upload(output, temp_path, folder, filename, course,
                                 digest, source_kind, source_role, ingest_run_id):
    # Hold this lock only in a worker thread. Both publication and queue metadata
    # must be visible before directory scans can discover the new archive.
    with ingest_jobs.registration():
        target = _publish_upload(output, temp_path, folder, filename)
        task = None
        if source_kind != 'development_doc':
            task = ingest_jobs.enqueue(course, target, folder, digest=digest,
                                       source_kind=source_kind, source_role=source_role,
                                       ingest_run_id=ingest_run_id)
        return target, task


def _intake_result(course: str, workspace_key: str, folder: Path,
                   target: Path, byte_count: int, *, source_kind: str = 'unclassified',
                   source_role: str = '', ingest_run_id: int | None = None) -> dict:
    """Scan one newly saved material and return the shared intake response."""
    scanned = sources.scan_course(course, [folder])
    file_status = next((item for item in scanned['files']
                        if item['path'] == str(target.resolve())), None)
    metadata = sources.source_metadata(source_kind, source_role)
    conn = db.connect()
    try:
        source_row = conn.execute(
            'SELECT id FROM source_files WHERE course_id=(SELECT id FROM courses WHERE name=?) AND path=?',
            (course, str(target.resolve()))
        ).fetchone()
        source_id = source_row['id'] if source_row else None
        if source_id:
            with conn:
                conn.execute('''UPDATE source_files SET source_kind=?,source_role=?,catalog_status=?,ingest_run_id=?
                                WHERE id=?''',
                             (metadata['source_kind'], metadata['source_role'], metadata['catalog_status'],
                              ingest_run_id, source_id))
        if ingest_run_id:
            with conn:
                conn.execute('''UPDATE ingest_runs SET source_id=?,status='completed',artifact_count=?,finished_at=datetime('now','localtime')
                                WHERE id=?''',
                             (source_id, 1 if file_status else 0, ingest_run_id))
    finally:
        conn.close()
    course_dir = folder.parent
    return {
        'ok': True,
        'course': course,
        'filename': target.name,
        'bytes': byte_count,
        'path': str(target.resolve()),
        'course_dir': str(course_dir.resolve()),
        'inbox_dir': str(folder.resolve()),
        'folder': f'{workspace_key}/{_safe_filename(course)}/inbox',
        'writable': os.access(folder, os.W_OK),
        'status': '已索引，可检索' if file_status and file_status['status'] == 'indexed' else
                  '已归档，等待 OCR/转写或格式支持' if target.suffix.lower() not in sources.SUPPORTED else
                  '已归档，抽取失败，请查看文件状态',
        'index': file_status,
        'source_kind': metadata['source_kind'],
        'source_role': metadata['source_role'],
        'catalog_status': metadata['catalog_status'],
        'ingest_run_id': ingest_run_id,
    }


def _start_ingest_run(course: str, source_kind: str, source_role: str) -> tuple[int, dict]:
    metadata = sources.source_metadata(source_kind, source_role)
    conn = db.connect()
    try:
        course_row = conn.execute('SELECT id FROM courses WHERE name=?', (course,)).fetchone()
        if not course_row:
            raise HTTPException(400, '请选择有效课程')
        with conn:
            cur = conn.execute('''INSERT INTO ingest_runs(course_id,source_kind,source_role,status)
                                 VALUES(?,?,?,'running')''',
                               (course_row['id'], metadata['source_kind'], metadata['source_role']))
        return cur.lastrowid, metadata
    finally:
        conn.close()


def _fail_ingest_run(run_id: int | None, error: str) -> None:
    if not run_id:
        return
    conn = db.connect()
    try:
        with conn:
            conn.execute("UPDATE ingest_runs SET status='failed',error=?,finished_at=datetime('now','localtime') WHERE id=?",
                         (str(error)[:500], run_id))
    finally:
        conn.close()


def _development_folder() -> Path:
    folder = db.DATA_DIR / 'app' / 'incoming' / 'development'
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@router.get('/runtime')
def runtime_info() -> dict:
    root = db.DATA_DIR.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return {
        'data_dir': str(root),
        'writable': os.access(root, os.W_OK),
        'platform': platform.system(),
        'mode': 'external' if os.environ.get('CYBERPAL_DATA_DIR', '').strip() else 'project-local',
    }


@router.post('/runtime/reveal')
def runtime_reveal(body: RevealIn) -> dict:
    path = _runtime_path(body.path)
    if not _inside_runtime_root(path):
        raise HTTPException(400, '只能打开当前应用数据目录内的文件或文件夹')
    if not path.exists():
        raise HTTPException(404, '文件或文件夹不存在')
    system = platform.system()
    try:
        if system == 'Darwin':
            subprocess.Popen(['open', '-R', str(path)])
        elif system == 'Windows':
            subprocess.Popen(['explorer', '/select,', str(path)])
        elif system == 'Linux':
            subprocess.Popen(['xdg-open', str(path if path.is_dir() else path.parent)])
        else:
            raise HTTPException(400, f'当前系统暂不支持打开目录：{system}')
    except OSError as exc:
        raise HTTPException(500, f'打开目录失败：{exc}') from exc
    return {'ok': True, 'path': str(path)}


# ── 数据与隐私诊断（T2.18a / D-10）──────────────────
@router.get('/diagnostics')
def diagnostics_read() -> dict:
    """脱敏的数据与隐私自查：相对名称、计数与摘要，不含绝对路径或密钥。"""
    return diagnostics_service.summary()


@router.post('/diagnostics/export')
def diagnostics_export() -> dict:
    """生成脱敏诊断包（zip）；返回文件名与大小，不返回绝对路径。"""
    try:
        return diagnostics_service.export()
    except ValueError:
        raise HTTPException(413, '诊断包超出大小限制') from None
    except OSError:
        raise HTTPException(500, '诊断包写入失败；请检查数据目录是否可写') from None


@router.get('/workspaces')
def workspaces() -> list[dict]:
    return repo.list_workspaces()


@router.post('/workspaces')
def workspace_create(body: WorkspaceIn) -> dict:
    try:
        workspace = repo.create_workspace(body.key, body.name, kind=body.kind,
                                          domain=body.domain, description=body.description)
        _workspace_folder(workspace['key']).mkdir(parents=True, exist_ok=True)
        return {'ok': True, 'workspace': workspace}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/workspaces/{workspace_key}/items')
def workspace_items(workspace_key: str, kind: str | None = None) -> list[dict]:
    if not repo.get_workspace(workspace_key):
        raise HTTPException(404, '工作区不存在')
    return repo.list_workspace_items(workspace_key, kind)


@router.post('/workspaces/items')
def workspace_item_create(body: WorkspaceItemIn) -> dict:
    try:
        return repo.create_workspace_item(body.workspace, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch('/workspaces/items/{item_id}')
def workspace_item_update(item_id: int, body: dict) -> dict:
    try:
        return repo.update_workspace_item(item_id, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete('/workspaces/items/{item_id}')
def workspace_item_delete(item_id: int) -> dict:
    return {'ok': repo.delete_workspace_item(item_id)}


@router.post("/intake")
def intake(body: IntakeIn) -> dict:
    """将上传材料保存到 StudyPal/<课程>/inbox，等待后续抽取。"""
    course = (body.course or '').strip()
    workspace_key, folder = _intake_folder(course)
    source_kind = sources.normalize_source_kind(body.source_kind)
    if source_kind == 'development_doc':
        raise HTTPException(400, '开发文档请使用原始流上传接口，不应进入课程材料入口')
    encoded = body.content_base64 or ""
    if "," in encoded and encoded.startswith("data:"):
        encoded = encoded.split(",", 1)[1]
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "文件内容无效") from exc
    if not content:
        raise HTTPException(400, "不能归档空文件")
    if len(content) > sources.MAX_BYTES:
        raise HTTPException(413, "单个文件不能超过 200 MB")
    ingest_run_id, _ = _start_ingest_run(course, source_kind, body.source_role)
    target = _next_intake_target(folder, body.filename)
    try:
        target.write_bytes(content)
        return _intake_result(course, workspace_key, folder, target, len(content),
                              source_kind=source_kind, source_role=body.source_role,
                              ingest_run_id=ingest_run_id)
    except Exception as exc:
        _fail_ingest_run(ingest_run_id, str(exc))
        raise


@router.post('/intake/upload')
async def intake_upload(request: Request, course: str, filename: str,
                        mime: str = 'application/octet-stream',
                        source_kind: str = 'unclassified', source_role: str = '') -> dict:
    """Receive a file as a raw stream so the browser never creates a Base64 copy."""
    del mime  # MIME is advisory; the extension and extracted content determine indexing.
    course = (course or '').strip()
    workspace_key, folder = await run_in_threadpool(_intake_folder, course)
    source_kind = sources.normalize_source_kind(source_kind)
    ingest_run_id, _ = await run_in_threadpool(_start_ingest_run, course, source_kind, source_role)
    if source_kind == 'development_doc':
        folder = await run_in_threadpool(_development_folder)
    target = folder / _safe_filename(filename)
    temp_path = folder / f'.{target.name}.{uuid.uuid4().hex}.upload'
    total = 0
    digest = hashlib.sha256()

    try:
        try:
            content_length = request.headers.get('content-length')
            if content_length and int(content_length) > sources.MAX_BYTES:
                raise HTTPException(413, '单个文件不能超过 200 MB')
        except ValueError:
            # Ignore malformed optional metadata; the streamed byte count remains authoritative.
            pass
        output = await run_in_threadpool(temp_path.open, 'wb')
        try:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > sources.MAX_BYTES:
                    raise HTTPException(413, '单个文件不能超过 200 MB')
                await run_in_threadpool(_write_upload_chunk, output, digest, chunk)
            if total == 0:
                raise HTTPException(400, '不能归档空文件')
            target, task = await run_in_threadpool(_publish_and_register_upload,
                output, temp_path, folder, filename, course, digest.hexdigest(),
                source_kind, source_role, ingest_run_id)
        finally:
            await run_in_threadpool(output.close)
        if source_kind == 'development_doc':
            conn = db.connect()
            try:
                with conn:
                    conn.execute("UPDATE ingest_runs SET status='completed',artifact_count=1,finished_at=datetime('now','localtime') WHERE id=?", (ingest_run_id,))
            finally:
                conn.close()
            return {
                'ok': True, 'indexed': False, 'course': course, 'filename': target.name,
                'bytes': total, 'path': str(target.resolve()),
                'folder': 'app/incoming/development', 'source_kind': source_kind,
                'source_role': sources.source_metadata(source_kind, source_role)['source_role'],
                'catalog_status': 'none', 'ingest_run_id': ingest_run_id,
                'status': '已保存到平台资料区，未进入课程图谱',
            }
        return {
            'ok': True, 'indexed': False, 'course': course, 'filename': target.name,
            'bytes': total, 'path': str(target.resolve()),
            'course_dir': str(folder.parent.resolve()), 'inbox_dir': str(folder.resolve()),
            'folder': f'{workspace_key}/{_safe_filename(course)}/inbox',
            'writable': os.access(folder, os.W_OK),
            'status': '已归档，内容重复，沿用已有材料任务' if task['deduplicated'] else '已归档，识别任务已排队',
            'task_id': task['id'], 'deduplicated': task['deduplicated'],
            'canonical_path': task['path'],
            'ingest_run_id': ingest_run_id, 'job': task,
            'source_kind': source_kind, 'source_role': sources.source_metadata(source_kind, source_role)['source_role'],
            'catalog_status': sources.source_metadata(source_kind, source_role)['catalog_status'],
        }
    except BaseException as exc:
        await run_in_threadpool(_fail_ingest_run, ingest_run_id, str(exc))
        raise
    finally:
        # The hidden staging file must never remain in the course inbox, including on 413.
        try:
            await run_in_threadpool(temp_path.unlink)
        except FileNotFoundError:
            pass


def scan_all_sources() -> dict:
    configured = settings_store.read_raw().get('local_sources', {})
    results = {}
    for course in repo.list_courses():
        name = course['name']
        extra = configured.get(name, []) if isinstance(configured, dict) else []
        if not isinstance(extra, list):
            extra = []
        folders = [_workspace_folder(course.get('workspace_key', 'studypal')) / _safe_filename(name) / 'inbox']
        folders += [Path(value).expanduser() for value in extra if isinstance(value, str)]
        results[name] = ingest_jobs.scan(name, folders)
    return results


@router.post('/sources/scan')
def scan_sources(course: str | None = None) -> dict:
    if course:
        if course not in {c['name'] for c in repo.list_courses()}:
            raise HTTPException(404, '课程不存在')
        extra = settings_store.read_raw().get('local_sources', {}).get(course, [])
        if not isinstance(extra, list):
            extra = []
        folders = [_workspace_folder(_course_workspace_key(course)) / _safe_filename(course) / 'inbox'] + [Path(v).expanduser() for v in extra]
        return {course: ingest_jobs.scan(course, folders)}
    return scan_all_sources()


@router.get('/ingest/jobs')
def ingest_job_list(course: str | None = None, workspace: str | None = None) -> list[dict]:
    return ingest_jobs.list_jobs(course=course, workspace=workspace)


@router.get('/ingest/jobs/{task_id}')
def ingest_job(task_id: int) -> dict:
    try:
        return ingest_jobs.get(task_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post('/ingest/jobs/{task_id}/cancel')
def ingest_job_cancel(task_id: int) -> dict:
    try:
        return ingest_jobs.cancel(task_id)
    except ingest_jobs.IngestBusyError as exc:
        # P1 (T2.11): indexing holds the SQLite write lock; ask the client to
        # retry instead of surfacing a 500 database-is-locked error.
        raise HTTPException(409, ingest_jobs.CANCEL_BUSY_ERROR, headers={'Retry-After': '2'}) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post('/ingest/jobs/{task_id}/retry')
def ingest_job_retry(task_id: int) -> dict:
    try:
        return ingest_jobs.retry(task_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/sources/ocr')
def reprocess_ocr(course: str) -> dict:
    """Explicitly retry extraction for files previously deferred for OCR."""
    if course not in {c['name'] for c in repo.list_courses()}:
        raise HTTPException(404, '课程不存在')
    extra = settings_store.read_raw().get('local_sources', {}).get(course, [])
    if not isinstance(extra, list):
        extra = []
    folders = [_workspace_folder(_course_workspace_key(course)) / _safe_filename(course) / 'inbox'] + [Path(v).expanduser() for v in extra]
    return {course: ingest_jobs.scan(course, folders, force_ocr=True)}


@router.get('/sources')
def source_status(course: str, workspace: str | None = None) -> dict:
    _require_course_scope(course, workspace)
    workspace_key = _course_workspace_key(course)
    course_dir = _workspace_folder(workspace_key) / _safe_filename(course)
    inbox_dir = course_dir / 'inbox'
    return {
        'data_dir': str(db.DATA_DIR.expanduser().resolve()),
        'course_dir': str(course_dir.resolve()),
        'inbox_dir': str(inbox_dir.resolve()),
        'writable': os.access(course_dir if course_dir.exists() else course_dir.parent, os.W_OK),
        'extractor': sources.extractor_status(),
        'files': sources.status(course),
        'materials': sources.inventory(course),
        'candidates': sources.candidates(course),
        'catalog': repo.catalog_sources(course),
    }


class CatalogSelectIn(BaseModel):
    course: str
    source_id: int


@router.post('/sources/catalog')
def select_catalog(body: CatalogSelectIn) -> dict:
    try:
        return repo.select_catalog_source(body.course, body.source_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/sources/candidates/{candidate_id}/confirm')
def confirm_candidate(candidate_id: int) -> dict:
    try:
        return sources.confirm_candidate(candidate_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post('/sources/candidates/{candidate_id}/review')
def review_candidate(candidate_id: int, body: dict) -> dict:
    try:
        return sources.review_candidate(candidate_id, body.get('action', ''), body.get('target_kp_id'), body.get('note', ''))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/sources/candidates/review-batch')
def review_source_candidates_batch(body: dict) -> dict:
    """Confirm or reject a bounded set of candidates from one source."""
    action = body.get('action', '')
    candidate_ids = body.get('candidate_ids', [])
    if action not in {'confirmed', 'rejected'}:
        raise HTTPException(400, '批量审核动作只能是 confirmed 或 rejected')
    if not isinstance(candidate_ids, list) or not all(isinstance(item, int) for item in candidate_ids):
        raise HTTPException(400, 'candidate_ids 必须是整数数组')
    if not candidate_ids or len(candidate_ids) > 200:
        raise HTTPException(400, '一次只能处理 1 到 200 个候选')
    results = []
    try:
        for candidate_id in dict.fromkeys(candidate_ids):
            if action == 'confirmed':
                results.append(sources.confirm_candidate(candidate_id))
            else:
                results.append(sources.review_candidate(candidate_id, 'rejected'))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {'ok': True, 'action': action, 'count': len(results), 'results': results}


class FolderIn(BaseModel):
    course: str
    path: str


class WikiDraftIn(BaseModel):
    course: str
    path: str
    tier: str = 'fast'


class SourceDraftDeleteIn(BaseModel):
    course: str
    path: str


class SourceRestoreIn(BaseModel):
    course: str
    path: str
    sha256: str


class HubRestoreIn(BaseModel):
    revision_id: int


class HubSkillIn(BaseModel):
    key: str
    name: str
    version: str = '1.0.0'
    description: str = ''
    content: str
    enabled: bool = True


class HubImportIn(BaseModel):
    content_base64: str
    filename: str = 'hub.zip'


@router.post('/sources/folders')
def add_source_folder(body: FolderIn) -> dict:
    if body.course not in {c['name'] for c in repo.list_courses()}:
        raise HTTPException(404, '课程不存在')
    folder = Path(body.path).expanduser().resolve()
    if not folder.is_dir():
        raise HTTPException(400, '目录不存在或不是文件夹')
    current = settings_store.read_raw().get('local_sources', {})
    paths = current.setdefault(body.course, [])
    if str(folder) not in paths:
        paths.append(str(folder))
        settings_store.write({'local_sources': current})
    return {'course': body.course, 'folders': paths}


@router.post('/sources/wiki-draft')
def source_wiki_draft(body: WikiDraftIn) -> dict:
    if body.tier not in ('fast', 'reasoning'):
        raise HTTPException(400, '模型档位只能是 fast 或 reasoning')
    try:
        return sources.assist_wiki(body.course, body.path, tier=body.tier)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f'辅助拆录失败：{str(exc)[:300]}') from exc


@router.post('/sources/wiki-draft/delete')
def delete_source_draft(body: SourceDraftDeleteIn) -> dict:
    try:
        return sources.delete_assisted_draft(body.course, body.path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/sources/versions')
def source_versions(course: str, path: str) -> list[dict]:
    try:
        return sources.versions(course, path)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get('/sources/diff')
def source_diff(course: str, path: str, from_sha: str, to_sha: str = 'latest') -> dict:
    try:
        return sources.diff_versions(course, path, from_sha, to_sha)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post('/sources/restore')
def source_restore(body: SourceRestoreIn) -> dict:
    try:
        return sources.restore_version(body.course, body.path, body.sha256)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/seed/refresh')
def refresh_seed() -> dict:
    try:
        return seed.refresh()
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


@router.get('/hub')
def hub_list() -> dict:
    try:
        return registry.list_registry()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/config-assistant/preview')
def config_assistant_preview(body: ConfigAssistantPreviewIn) -> dict:
    try:
        return config_composer.preview(body.prompt, target=body.target)
    except llm.LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/config-assistant/confirm')
def config_assistant_confirm(body: dict) -> dict:
    try:
        return config_composer.confirm(body)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/hub/agent', status_code=201)
def hub_agent_create(body: dict) -> dict:
    try:
        return registry.create_agent(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/hub/skill', status_code=201)
def hub_skill_create(body: HubSkillIn) -> dict:
    try:
        return registry.create_skill(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/hub/export')
def hub_export() -> Response:
    try:
        payload = registry.export_bundle()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(content=payload, media_type='application/zip',
                    headers={'Content-Disposition': 'attachment; filename="cyberpal-hub.zip"'})


@router.post('/hub/import')
def hub_import(body: HubImportIn) -> dict:
    encoded = body.content_base64.split(',', 1)[-1]
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, 'Hub 压缩包内容无效') from exc
    try:
        return registry.import_bundle(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/workflows')
def workflow_list(workspace_key: str | None = None) -> list[dict]:
    if workspace_key is not None and (not isinstance(workspace_key, str) or len(workspace_key) > 64):
        raise HTTPException(400, 'workspace_key 长度无效')
    workflows = _workflow_call(workflow_service.WorkflowService().list, workspace_key)
    return [_public_workflow(item) for item in workflows]


@router.post('/workflows', status_code=201)
def workflow_create(body: WorkflowCreateIn) -> dict:
    payload = body.model_dump(exclude={'template', 'trigger_json'}, exclude_none=True)
    if body.trigger_json is not None:
        payload['trigger_json'] = body.trigger_json
    created = None
    try:
        _reject_workflow_secrets(payload)
        created = workflow_service.WorkflowService().create(payload)
        if body.template:
            created = workflow_service.WorkflowService().create_from_template(
                body.template, workflow_id=created['id'])
        return _public_workflow(created)
    except ValueError as exc:
        # A template failure must not leave a half-created definition behind.
        if body.template and created is not None:
            try:
                workflow_service.WorkflowService().delete(created['key'])
            except Exception:
                pass
        raise _workflow_http_error(exc) from exc
    except (sqlite3.Error, TypeError) as exc:
        raise HTTPException(400, '工作流请求无效') from exc


@router.get('/workflows/import')
def workflow_import_help() -> dict:
    return {'method': 'POST', 'path': '/api/workflows/import',
            'description': '只导入工作流元数据、步骤配置以及 Agent/Skill key'}


@router.post('/workflows/import', status_code=201)
def workflow_import(body: WorkflowImportIn) -> dict:
    steps = [step.model_dump(exclude={'position'}) for step in body.steps]
    payload = {
        'key': body.key,
        'name': body.name,
        'workspace_key': body.workspace_key,
        'description': body.description,
        'trigger': body.trigger,
        'status': body.status,
    }
    created = None
    try:
        _reject_workflow_secrets({'workflow': payload, 'steps': steps})
        _validate_workflow_dependencies(steps)
        service = workflow_service.WorkflowService()
        created = service.create(payload)
        for step in sorted(body.steps, key=lambda item: item.position if item.position is not None else 10_000):
            service.add_step(body.key, step.model_dump(exclude={'position'}))
        return _public_workflow(service.get(body.key))
    except ValueError as exc:
        if created is not None:
            try:
                workflow_service.WorkflowService().delete(body.key)
            except Exception:
                pass
        raise _workflow_http_error(exc) from exc
    except (sqlite3.Error, TypeError) as exc:
        if created is not None:
            try:
                workflow_service.WorkflowService().delete(body.key)
            except Exception:
                pass
        raise HTTPException(400, '工作流导入失败') from exc


@router.get('/workflows/{key}')
def workflow_get(key: str) -> dict:
    workflow = _workflow_call(workflow_service.WorkflowService().get, key)
    return _public_workflow(workflow)


@router.patch('/workflows/{key}')
def workflow_update(key: str, body: WorkflowUpdateIn) -> dict:
    payload = body.model_dump(exclude_unset=True, exclude={'trigger_json'}, exclude_none=True)
    if body.trigger_json is not None:
        payload['trigger_json'] = body.trigger_json
    try:
        _reject_workflow_secrets(payload)
        workflow = _workflow_call(workflow_service.WorkflowService().update, key, payload)
        return _public_workflow(workflow)
    except ValueError as exc:
        raise _workflow_http_error(exc) from exc


@router.delete('/workflows/{key}')
def workflow_delete(key: str) -> dict:
    _workflow_call(workflow_service.WorkflowService().delete, key)
    return {'ok': True}


@router.post('/workflows/{key}/steps', status_code=201)
def workflow_step_create(key: str, body: WorkflowStepCreateIn) -> dict:
    payload = body.model_dump()
    try:
        _reject_workflow_secrets(payload)
        step = _workflow_call(workflow_service.WorkflowService().add_step, key, payload)
        return _scrub_workflow_output(step)
    except ValueError as exc:
        raise _workflow_http_error(exc) from exc


@router.patch('/workflows/{key}/steps/{step_key}')
def workflow_step_update(key: str, step_key: str, body: WorkflowStepUpdateIn) -> dict:
    payload = body.model_dump(exclude_unset=True, exclude_none=True)
    try:
        _reject_workflow_secrets(payload)
        step = _workflow_call(workflow_service.WorkflowService().update_step, key, step_key, payload)
        return _scrub_workflow_output(step)
    except ValueError as exc:
        raise _workflow_http_error(exc) from exc


@router.post('/workflows/{key}/steps/reorder')
def workflow_steps_reorder(key: str, body: WorkflowReorderIn) -> list[dict]:
    try:
        steps = _workflow_call(workflow_service.WorkflowService().reorder_steps, key, body.ordered_keys)
        return [_scrub_workflow_output(step) for step in steps]
    except ValueError as exc:
        raise _workflow_http_error(exc) from exc


@router.delete('/workflows/{key}/steps/{step_key}')
def workflow_step_delete(key: str, step_key: str) -> dict:
    _workflow_call(workflow_service.WorkflowService().delete_step, key, step_key)
    return {'ok': True}


@router.get('/workflows/{key}/export')
def workflow_export(key: str) -> dict:
    exported = _workflow_call(workflow_service.WorkflowService().export_config, key)
    _reject_workflow_secrets(exported)
    return _scrub_workflow_output(exported)


@router.post('/workflows/{key}/runs', status_code=201)
def workflow_run_start(key: str, body: WorkflowRunIn) -> dict:
    try:
        payload = body.model_dump()
        _reject_workflow_secrets(payload)
        # Runs execute on a background thread; this request only waits for a
        # short first-state grace period and returns the run for polling.
        run = _workflow_call(workflow_service.WorkflowService().start_run_async,
                             key, body.input_data, body.trigger_type)
        return _public_run(run)
    except ValueError as exc:
        raise _workflow_http_error(exc) from exc


@router.get('/workflows/{key}/runs')
def workflow_run_list(key: str, limit: int = 50) -> list[dict]:
    runs = _workflow_call(workflow_service.WorkflowService().list_runs, key, limit)
    return [_public_run(run) for run in runs]


@router.get('/workflow-runs/{run_id}')
def workflow_run_get(run_id: int) -> dict:
    run = _workflow_call(workflow_service.WorkflowService().get_run, run_id)
    return _public_run(run)


@router.post('/workflow-runs/{run_id}/approve')
def workflow_run_approve(run_id: int, body: WorkflowApprovalIn) -> dict:
    # Approved runs continue on the background worker and are polled by the UI.
    run = _workflow_call(workflow_service.WorkflowService().approve_run_async, run_id, body.decision)
    return _public_run(run)


@router.post('/workflow-runs/{run_id}/cancel')
def workflow_run_cancel(run_id: int) -> dict:
    # cancel_run intentionally reports one generic invalid-state error; look
    # up first so an unknown run still follows the API's 404 contract.
    _workflow_call(workflow_service.WorkflowService().get_run, run_id)
    run = _workflow_call(workflow_service.WorkflowService().cancel_run, run_id)
    return _public_run(run)


@router.post('/workflow-runs/{run_id}/retry')
def workflow_run_retry(run_id: int) -> dict:
    # Retried runs resume on the background worker and are polled by the UI.
    run = _workflow_call(workflow_service.WorkflowService().retry_run_async, run_id)
    return _public_run(run)


@router.get('/hub/runs')
def hub_runs(limit: int = 50) -> list[dict]:
    conn = db.connect()
    try:
        return [dict(row) for row in conn.execute('SELECT * FROM hub_runs ORDER BY id DESC LIMIT ?', (max(1, min(limit, 200)),)).fetchall()]
    finally:
        conn.close()


# ── Personal Agent：今日、待处理与继续执行 ──────────────────────
def _agent_runtime() -> PersonalAgentService:
    return PersonalAgentService()


def _agent_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _agent_workspace(workspace: str) -> dict:
    key = (workspace or 'studypal').strip()
    item = repo.get_workspace(key)
    if not item:
        raise HTTPException(400, '工作区不存在')
    return item


def _decode_agent_action(action: dict) -> dict:
    result = dict(action)
    for field, target in (('proposal_json', 'proposal'), ('before_json', 'before'), ('after_json', 'after')):
        try:
            result[target] = json.loads(result.pop(field, '{}') or '{}')
        except (TypeError, json.JSONDecodeError):
            result[target] = {}
    return result


def _agent_today_payload(workspace: str) -> dict:
    ws = _agent_workspace(workspace)
    runtime = _agent_runtime()
    today = datetime.now().date()
    horizon = today + timedelta(days=14)
    events = []
    for item in repo.list_events(workspace=ws['key']):
        date_text = str(item.get('date') or '')[:10]
        try:
            event_date = datetime.strptime(date_text, '%Y-%m-%d').date()
        except ValueError:
            continue
        if today <= event_date <= horizon:
            item = dict(item)
            item['source_id'] = f"event:{item.get('id')}"
            events.append(item)
    goals = runtime.list_goals(workspace_key=ws['key'], status='active')[:8]
    pending = [_decode_agent_action(item) for item in ActionService().pending(workspace_key=ws['key'])]
    runs = runtime.list_runs(
        workspace_key=ws['key'],
        statuses={'queued', 'running', 'waiting_approval'},
        limit=12,
    )
    return {
        'workspace': ws,
        'today': today.isoformat(),
        'horizon': horizon.isoformat(),
        'events': events[:30],
        'goals': goals,
        'pending_actions': pending[:12],
        'active_runs': runs,
    }


@router.get('/agent/profile')
def agent_profile_read() -> dict:
    return _agent_runtime().get_profile()


@router.put('/agent/profile')
def agent_profile_write(body: AgentProfileIn) -> dict:
    if body.active_workspace_key is not None:
        _agent_workspace(body.active_workspace_key)
    return _agent_call(
        _agent_runtime().update_profile,
        display_name=body.display_name,
        memory_policy=body.memory_policy,
        preferences=body.preferences,
        active_workspace_key=body.active_workspace_key,
    )


@router.get('/agent/goals')
def agent_goals(workspace: str = 'studypal', status: str | None = None) -> list[dict]:
    _agent_workspace(workspace)
    if status is not None and status not in {'active', 'paused', 'completed', 'archived'}:
        raise HTTPException(400, '目标状态无效')
    return _agent_call(_agent_runtime().list_goals, workspace_key=workspace, status=status)


@router.post('/agent/goals', status_code=201)
def agent_goal_create(body: AgentGoalIn) -> dict:
    _agent_workspace(body.workspace_key)
    return _agent_call(
        _agent_runtime().create_goal,
        workspace_key=body.workspace_key,
        title=body.title,
        description=body.description,
        priority=body.priority,
        due_at=body.due_at,
        next_action=body.next_action,
        source_type=body.source_type,
        source_id=body.source_id,
    )


@router.patch('/agent/goals/{goal_id}')
def agent_goal_update(goal_id: int, body: dict, workspace: str = 'studypal') -> dict:
    _agent_workspace(workspace)
    return _agent_call(_agent_runtime().update_goal, goal_id, workspace_key=workspace, values=body)


@router.get('/agent/today')
def agent_today(workspace: str = 'studypal') -> dict:
    return _agent_today_payload(workspace)


@router.get('/agent/attention')
def agent_attention(workspace: str = 'studypal') -> dict:
    ws = _agent_workspace(workspace)
    actions = [_decode_agent_action(item) for item in ActionService().pending(workspace_key=ws['key'])]
    runs = _agent_runtime().list_runs(
        workspace_key=ws['key'],
        statuses={'queued', 'running', 'waiting_approval', 'failed'},
        limit=30,
    )
    source_candidates: list[dict] = []
    relation_candidates: list[dict] = []
    if ws['key'] == 'studypal':
        for course in repo.list_courses(ws['key']):
            for candidate in sources.candidates(course['name']):
                if candidate.get('status') != 'pending':
                    continue
                item = dict(candidate)
                item['course_name'] = course['name']
                source_candidates.append(item)
        course_names = {course['name'] for course in repo.list_courses(ws['key'])}
        for candidate in repo.list_relation_candidates(status='pending'):
            if candidate.get('from_course') not in course_names and candidate.get('to_course') not in course_names:
                continue
            item = dict(candidate)
            from_kp = repo.get_kp(candidate.get('from_kp')) if candidate.get('from_kp') else None
            to_kp = repo.get_kp(candidate.get('to_kp')) if candidate.get('to_kp') else None
            item['from_name'] = from_kp.get('name') if from_kp else ''
            item['to_name'] = to_kp.get('name') if to_kp else ''
            relation_candidates.append(item)
    return {
        'workspace': ws,
        'actions': actions,
        'runs': runs,
        'source_candidates': source_candidates[:20],
        'relation_candidates': relation_candidates[:20],
    }


@router.post('/agent/runs', status_code=201)
def agent_run_create(body: AgentRunIn) -> dict:
    _agent_workspace(body.workspace_key)
    return _agent_call(
        _agent_runtime().create_run,
        workspace_key=body.workspace_key,
        agent_key=body.agent_key,
        trigger_type=body.trigger_type,
        trigger_id=body.trigger_id,
        input_data=body.input,
        context=body.context,
        dedupe_key=body.dedupe_key,
    )


@router.get('/agent/runs')
def agent_runs(workspace: str = 'studypal', status: str | None = None, limit: int = 30) -> list[dict]:
    _agent_workspace(workspace)
    statuses = {status} if status else None
    return _agent_call(_agent_runtime().list_runs, workspace_key=workspace, statuses=statuses, limit=limit)


@router.get('/agent/runs/{run_id}')
def agent_run_get(run_id: int, workspace: str = 'studypal') -> dict:
    _agent_workspace(workspace)
    result = _agent_runtime().get_run(run_id, workspace_key=workspace)
    if not result:
        raise HTTPException(404, '运行记录不存在')
    result.pop('input_json', None)
    result.pop('context_json', None)
    return result


def _agent_action_executor(workspace: str):
    def execute(payload: dict) -> dict:
        action_workspace = str(payload.get('workspace_key') or payload.get('workspace') or workspace)
        if action_workspace != workspace:
            raise ValueError('动作不属于当前工作区')
        kind = str(payload.get('action_kind') or payload.get('kind') or '')
        if kind == 'create_workspace_item':
            return repo.create_workspace_item(workspace, payload)
        if kind == 'update_workspace_item':
            item_id = int(payload.get('item_id') or payload.get('target_id') or 0)
            items = {int(item['id']): item for item in repo.list_workspace_items(workspace)}
            if item_id not in items:
                raise ValueError('工作区记录不存在或不属于当前工作区')
            return repo.update_workspace_item(item_id, payload)
        if kind == 'create_event':
            values = dict(payload)
            values.pop('workspace_key', None)
            values['workspace_id'] = _agent_workspace(workspace)['id']
            return repo.create_event(values, origin='agent')
        if kind == 'update_event':
            event_id = int(payload.get('event_id') or payload.get('target_id') or 0)
            events = {int(item['id']): item for item in repo.list_events(workspace=workspace) if str(item.get('id', '')).isdigit()}
            if event_id not in events:
                raise ValueError('日程不存在或不属于当前工作区')
            return repo.update_event(event_id, payload, origin='agent')
        if kind == 'update_mastery':
            kp_id = int(payload.get('kp_id') or payload.get('target_id') or 0)
            mastery = str(payload.get('mastery') or '')
            if mastery not in {'熟练', '会用', '读过', '未读'}:
                raise ValueError('掌握度只能是 熟练 / 会用 / 读过 / 未读')
            kp = repo.get_kp(kp_id)
            if not kp or not repo.get_workspace(workspace):
                raise ValueError('知识点不存在')
            course = next((item for item in repo.list_courses(workspace) if item['name'] == kp.get('course_name')), None)
            if not course:
                raise ValueError('知识点不属于当前工作区')
            return repo.write_mastery(kp_id, mastery, origin='agent', confirmed=True)
        if kind == 'save_memory_block':
            values = dict(payload)
            values.pop('workspace_key', None)
            return repo.create_memory_block(workspace, values, origin='agent')
        if kind == 'confirm_source_candidate':
            candidate_id = int(payload.get('candidate_id') or payload.get('target_id') or 0)
            course = str(payload.get('course') or '')
            if course not in {item['name'] for item in repo.list_courses(workspace)}:
                raise ValueError('候选不属于当前工作区')
            candidate = next((item for item in sources.candidates(course) if int(item['id']) == candidate_id), None)
            if not candidate:
                raise ValueError('知识候选不存在或不属于当前工作区')
            return sources.confirm_candidate(candidate_id)
        if kind == 'confirm_relation_candidate':
            candidate_id = int(payload.get('candidate_id') or payload.get('target_id') or 0)
            course_names = {item['name'] for item in repo.list_courses(workspace)}
            candidate = next((item for item in repo.list_relation_candidates(status='pending')
                              if int(item['id']) == candidate_id and (item.get('from_course') in course_names or item.get('to_course') in course_names)), None)
            if not candidate:
                raise ValueError('关系候选不存在或不属于当前工作区')
            return repo.review_relation_candidate(candidate_id, 'confirmed', origin='agent')
        if kind == 'save_web_source':
            return web_save(WebSaveIn(**payload))
        if kind == 'create_workflow_run':
            workflow_key = str(payload.get('workflow_key') or '').strip()
            workflow = workflow_service.WorkflowService().get(workflow_key)
            if workflow.get('workspace_key') != workspace:
                raise ValueError('工作流不属于当前工作区')
            run = workflow_service.WorkflowService().start_run(
                workflow_key,
                payload.get('input_data') if isinstance(payload.get('input_data'), dict) else {},
                str(payload.get('trigger_type') or 'agent'),
            )
            if run.get('status') == 'error':
                return {'ok': False, 'error': run.get('error') or '工作流执行失败'}
            return run
        if kind == 'save_conversation':
            conversation_id = int(payload.get('conversation_id') or payload.get('target_id') or 0)
            conversation = chat_repo.get_conversation(conversation_id)
            if not conversation or conversation.get('workspace_key') != workspace:
                raise ValueError('会话不存在或不属于当前工作区')
            return chat_repo.add_message(
                conversation_id,
                str(payload.get('role') or 'assistant'),
                str(payload.get('content') or '')[:20000],
                role_name=str(payload.get('role_name') or ''),
                scope_label=str(payload.get('scope_label') or ''),
                engine='agent',
                metadata={'source': 'personal-agent'},
            )
        raise ValueError(f'暂不支持确认动作：{kind or "未命名动作"}')
    return execute


@router.get('/agent/actions')
def agent_actions(workspace: str = 'studypal', status: str = 'proposed') -> list[dict]:
    _agent_workspace(workspace)
    if status not in {'proposed', 'approved', 'rejected', 'applied', 'failed', 'cancelled', 'all'}:
        raise HTTPException(400, '动作状态无效')
    if status == 'proposed':
        rows = ActionService().pending(workspace_key=workspace)
    else:
        conn = db.connect()
        try:
            sql = 'SELECT * FROM agent_actions WHERE workspace_key=?'
            args: list[Any] = [workspace]
            if status != 'all':
                sql += ' AND status=?'
                args.append(status)
            sql += ' ORDER BY id DESC LIMIT 100'
            rows = [dict(row) for row in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()
    return [_decode_agent_action(row) for row in rows]


@router.post('/agent/actions/{action_id}/approve')
def agent_action_approve(action_id: int, workspace: str = 'studypal') -> dict:
    _agent_workspace(workspace)
    result = _agent_call(ActionService().approve, action_id, workspace_key=workspace,
                         executor=_agent_action_executor(workspace))
    return _decode_agent_action(result)


@router.post('/agent/actions/{action_id}/reject')
def agent_action_reject(action_id: int, workspace: str = 'studypal') -> dict:
    _agent_workspace(workspace)
    return _decode_agent_action(_agent_call(ActionService().reject, action_id, workspace_key=workspace))


@router.get('/hub/{kind}/{key}')
def hub_detail(kind: str, key: str) -> dict:
    try:
        return registry.get_detail(kind, key)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get('/hub/{kind}/{key}/revisions')
def hub_revisions(kind: str, key: str) -> list[dict]:
    try:
        return registry.list_revisions(kind, key)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post('/hub/revisions/restore')
def hub_restore(body: HubRestoreIn) -> dict:
    try:
        return registry.restore_revision(body.revision_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch('/hub/{kind}/{key}')
def hub_toggle(kind: str, key: str, body: dict) -> dict:
    try:
        if set(body) <= {'enabled'}:
            return registry.set_enabled(kind, key, bool(body.get('enabled', True)))
        return registry.update(kind, key, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ── 分层个人记忆：规则、偏好、经历与领域知识 ─────────────
@router.get('/memory/blocks')
def memory_blocks(workspace: str = 'studypal', layer: str | None = None,
                  status: str | None = 'active', limit: int = 100) -> list[dict]:
    _agent_workspace(workspace)
    try:
        return repo.list_memory_blocks(workspace, layer=layer, status=status, limit=limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/memory/blocks', status_code=201)
def memory_block_create(body: MemoryBlockIn) -> dict:
    _agent_workspace(body.workspace_key)
    try:
        return repo.create_memory_block(body.workspace_key, body.model_dump(), origin='manual')
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/memory/blocks/{memory_id}')
def memory_block_detail(memory_id: int) -> dict:
    item = repo.get_memory_block(memory_id)
    if not item:
        raise HTTPException(404, '记忆不存在')
    return item


@router.patch('/memory/blocks/{memory_id}')
def memory_block_update(memory_id: int, body: dict) -> dict:
    try:
        return repo.update_memory_block(memory_id, body, origin='manual')
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete('/memory/blocks/{memory_id}')
def memory_block_archive(memory_id: int) -> dict:
    try:
        return repo.archive_memory_block(memory_id, origin='manual')
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/memory/blocks/{memory_id}/revisions')
def memory_block_revisions(memory_id: int) -> list[dict]:
    try:
        return repo.list_memory_revisions(memory_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


# ── 记忆：容器 ──────────────────────────────────────
@router.get("/courses")
def courses(workspace: str | None = None) -> list[dict]:
    return repo.list_courses(workspace)


@router.post("/courses")
def course_create(body: CourseIn) -> dict:
    """创建课程并同步建立该课程的本地材料入口。"""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "课程名称不能为空")
    if _safe_filename(name) != name:
        raise HTTPException(400, "课程名称不能包含路径或文件名特殊字符")
    workspace_key = body.workspace_key or 'studypal'
    if not repo.get_workspace(workspace_key):
        raise HTTPException(400, '工作区不存在')
    folder = _workspace_folder(workspace_key) / name / "inbox"
    created_folder = not folder.exists()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        course = repo.create_course(
            name,
            teacher=body.teacher,
            nature=body.nature,
            credits=body.credits,
            note=body.note,
            workspace=workspace_key,
        )
    except ValueError as exc:
        if created_folder:
            folder.rmdir()
            folder.parent.rmdir()
        raise HTTPException(400, str(exc)) from exc
    except (sqlite3.IntegrityError, TypeError) as exc:
        if created_folder:
            folder.rmdir()
            folder.parent.rmdir()
        raise HTTPException(400, "课程信息无效") from exc
    except OSError as exc:
        raise HTTPException(500, "无法创建课程存储目录，请检查本地写入权限") from exc
    return {
        "ok": True,
        "course": course,
        "folder": f"{workspace_key}/{_safe_filename(name)}/inbox",
    }


# ── 记忆：单元 ──────────────────────────────────────
@router.get("/kp")
def kp_list(course: str | None = None, band: str | None = None,
            mastery: str | None = None, freq: str | None = None,
            workspace: str | None = None) -> list[dict]:
    return repo.list_kp(course, band, mastery, freq, workspace)


@router.get("/kp/{kp_id}")
def kp_detail(kp_id: int) -> dict:
    d = repo.get_kp(kp_id)
    if not d:
        raise HTTPException(404, "知识点不存在")
    return d


@router.get('/kp/{kp_id}/neighbors')
def kp_neighbors(kp_id: int, depth: int = 1, kind: str | None = None) -> dict:
    try:
        return repo.kp_neighbors(kp_id, depth=depth, kind=kind)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get('/kp/{kp_id}/backlinks')
def kp_backlinks(kp_id: int) -> dict:
    try:
        result = repo.kp_neighbors(kp_id, depth=1)
        incoming = {path['path'][1] for path in result['paths']
                    if path['depth'] == 1 and path['direction'] == 'incoming' and len(path['path']) > 1}
        result['nodes'] = [node for node in result['nodes'] if node['id'] in incoming]
        result['paths'] = [path for path in result['paths'] if path['direction'] == 'incoming']
        result['mode'] = 'backlinks'
        return result
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get('/kp/{kp_id}/evidence')
def kp_evidence(kp_id: int) -> list[dict]:
    try:
        return repo.kp_evidence(kp_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get('/kp/{kp_id}/revisions')
def kp_revisions(kp_id: int) -> list[dict]:
    try:
        return repo.kp_revisions(kp_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


class MasteryIn(BaseModel):
    mastery: str
    origin: str = "chat"
    confirmed: bool = True


@router.post("/kp/{kp_id}/mastery")
def kp_write_mastery(kp_id: int, body: MasteryIn) -> dict:
    """写回掌握度 —— 闭环的第 ⑥ 环。留痕由 repo 保证。"""
    if body.mastery not in ("熟练", "会用", "读过", "未读"):
        raise HTTPException(400, "掌握度只能是 熟练 / 会用 / 读过 / 未读")
    return repo.write_mastery(kp_id, body.mastery, body.origin, body.confirmed)


class KpPatch(BaseModel):
    name: str | None = None
    band: str | None = None
    chapter: str | None = None
    gist: str | None = None
    detail: str | None = None
    trap: str | None = None
    source: str | None = None
    related: str | None = None
    kp_type: str | None = None
    freq: str | None = None


class KpContractPatch(BaseModel):
    domain: str | None = None
    workspace: str = ''
    attributes: dict = {}
    aliases: list[str] = []
    evidence_ids: list[int] = []


@router.put("/kp/{kp_id}")
def kp_update(kp_id: int, body: KpPatch) -> dict:
    try:
        return repo.update_kp(
            kp_id,
            {key: value for key, value in body.model_dump().items() if value is not None},
            origin="manual-graph",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put('/kp/{kp_id}/contract')
def kp_contract_update(kp_id: int, body: KpContractPatch) -> dict:
    try:
        return repo.update_node_contract(kp_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class EdgeIn(BaseModel):
    from_kp: int
    to_kp: int
    kind: str = "related"


class GraphCandidateExtractIn(BaseModel):
    course: str
    mode: str = 'explicit'


@router.post("/graph/edges")
def graph_edge_create(body: EdgeIn) -> dict:
    try:
        return repo.create_edge(body.from_kp, body.to_kp, body.kind)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/graph/edges/{edge_id}")
def graph_edge_delete(edge_id: int) -> dict:
    return repo.delete_edge(edge_id)


@router.get('/graph/candidates')
def graph_candidates(course: str | None = None, status: str = 'pending') -> list[dict]:
    return repo.list_relation_candidates(course, status)


@router.post('/graph/candidates/extract')
def graph_candidates_extract(body: GraphCandidateExtractIn) -> dict:
    try:
        if body.mode == 'ai':
            return relation_suggestions.generate_ai(body.course)
        if body.mode != 'explicit':
            raise ValueError('关系提取模式只能是 explicit 或 ai')
        return relation_suggestions.generate(body.course)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/graph/candidates/{candidate_id}/review')
def graph_candidate_review(candidate_id: int, body: dict) -> dict:
    try:
        return repo.review_relation_candidate(candidate_id, body.get('action', ''))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/graph/candidates/review-batch')
def graph_candidate_review_batch(body: dict) -> dict:
    action = body.get('action', 'confirmed')
    candidate_ids = body.get('candidate_ids', [])
    if not isinstance(candidate_ids, list) or not all(isinstance(item, int) for item in candidate_ids):
        raise HTTPException(400, 'candidate_ids 必须是整数数组')
    if len(candidate_ids) > 500:
        raise HTTPException(400, '单次最多处理 500 条关系候选')
    try:
        results = [repo.review_relation_candidate(item, action) for item in candidate_ids]
        return {'ok': True, 'action': action, 'count': len(results), 'results': results}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ── 记忆：状态 ──────────────────────────────────────
@router.get("/mastery")
def mastery(course: str, workspace: str | None = None) -> dict:
    if workspace is not None:
        _require_course_scope(course, workspace)
    return repo.mastery_matrix(course)


# ── 记忆：结构 ──────────────────────────────────────
@router.get("/graph")
def graph(course: str, workspace: str | None = None) -> dict:
    if workspace is not None:
        _require_course_scope(course, workspace)
    return repo.graph_of(course)


@router.get('/graph/health')
def graph_health(course: str | None = None) -> dict:
    return graph_tools.health(course)


@router.get('/knowledge/profile')
def knowledge_profile(course: str = '', workspace: str | None = None) -> dict:
    if course and workspace is not None:
        _require_course_scope(course, workspace)
    return domain_profiles.public_profile(course, workspace or '')


@router.get('/graph/export')
def graph_export(course: str | None = None) -> dict:
    return graph_tools.export_graph(course)


@router.post('/graph/import')
def graph_import(body: dict, course: str | None = None) -> dict:
    try:
        return graph_tools.import_graph(body, course=course)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ── 记忆：时间投影 ──────────────────────────────────
@router.get("/events")
def events(course: str | None = None, workspace: str | None = None) -> list[dict]:
    return repo.list_events(course, workspace)


@router.get("/notices")
def notices(course: str | None = None, workspace: str | None = None) -> list[dict]:
    return repo.list_notices(course, workspace)


@router.post("/events")
def event_create(body: EventIn) -> dict:
    try:
        values = body.model_dump()
        workspace_key = values.pop('workspace_key')
        if 'workspace_key' in body.model_fields_set:
            workspace = repo.get_workspace(workspace_key)
            if not workspace:
                raise ValueError('工作区不存在')
            values['workspace_id'] = workspace['id']
        return repo.create_event(values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/events/{event_id}")
def event_update(event_id: int, body: dict) -> dict:
    try:
        return repo.update_event(event_id, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/events/{event_id}")
def event_delete(event_id: int) -> dict:
    return {'ok': repo.delete_event(event_id)}


@router.get('/calendar/export.ics')
def calendar_export(course: str | None = None, workspace: str | None = None) -> Response:
    return Response(content=calendar_sync.export_ics(course, workspace), media_type='text/calendar; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="cyberpal-calendar.ics"'})


@router.post('/calendar/import.ics')
def calendar_import(body: dict) -> dict:
    text = str(body.get('content') or '')
    if not text:
        raise HTTPException(400, 'ICS 内容不能为空')
    try:
        return calendar_sync.import_ics(text, workspace=str(body.get('workspace') or 'studypal'))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/calendar/conflicts')
def calendar_conflicts(status: str = 'pending', workspace: str | None = None) -> list[dict]:
    return calendar_sync.conflicts(status, workspace)


@router.post('/calendar/conflicts/{conflict_id}/resolve')
def calendar_conflict_resolve(conflict_id: int, body: dict) -> dict:
    try:
        return calendar_sync.resolve_conflict(conflict_id, str(body.get('choice') or ''))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/agenda")
def agenda(q: str, course: str | None = None, workspace: str | None = None) -> dict:
    """Natural-language lookup over the structured calendar/task memory."""
    question = (q or "").strip()
    if not question:
        raise HTTPException(400, "查询内容不能为空")
    return repo.query_agenda(question, course, workspace)


# ── 记忆：关系（跨课桥接） ───────────────────────────
@router.get("/bridges")
def bridges(course: str | None = None) -> list[dict]:
    return repo.list_bridges(course)


# ── 写回留痕 ────────────────────────────────────────
@router.get("/revisions")
def revisions(limit: int = 30) -> list[dict]:
    return repo.list_revisions(limit)


# ── 设置：模型与接口 ────────────────────────────────
@router.get("/settings")
def settings_read() -> dict:
    """读配置。密钥脱敏，只回配置状态。"""
    return {
        "config": settings_store.read_public(),
        "configured": settings_store.configured(),
        "extensions": _extension_status(),
    }


@router.put("/settings")
def settings_write(patch: dict) -> dict:
    """局部更新配置。传空字符串 = 清除；不传 = 不动。"""
    try:
        config = settings_store.write(patch)
    except settings_store.RevisionConflict:
        raise HTTPException(409, '配置已变更，请重新载入后保存') from None
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        raise HTTPException(422, '配置无效或无法保存；模型列表请使用模型管理入口') from None
    return {
        "config": config,
        "configured": settings_store.configured(),
        "extensions": _extension_status(),
    }


def _extension_status() -> dict:
    from ..integrations import mcp_bridge
    from ..memory import vector_index
    return {'mcp': mcp_bridge.status(), 'vector': vector_index.status()}


@router.post("/settings/test")
def settings_test(body: dict | None = None) -> dict:
    """连通性自检：真发一次最小请求。
    「配不了模型」这种问题，要让界面给出确定结论，而不是靠猜。"""
    tier = (body or {}).get("tier", "fast")
    return llm.probe(tier)


# ── 检索即记忆 ──────────────────────────────────────
@router.get("/search")
def search(q: str, limit: int = 10, course: str | None = None, workspace: str | None = None,
           include_dev: int = 0, tag: str | None = None) -> dict:
    """course 不传 = 跨全部课程（全局范围）；传了 = 只在那门课里找。

    include_dev=1 仅用于调试开发文档排除（O-DATA-7），默认关闭。
    tag= 只在打了该标签的 kp/document 命中里求交；材料块等不可打标签的
    evidence/source_chunk/event 命中会被过滤（O-KB-5）。
    """
    include = bool(include_dev)
    # O-DATA-6: one deduped surface across FTS/LIKE/fallback for API clients and
    # /ask; hits keep their explicit truncated flag so clipped snippets are known.
    hits = repo.memory_search_hits(q, limit, course, workspace, include_dev=include)
    if tag not in (None, ''):
        try:
            tagged = documents.tagged_refs(tag, workspace)
        except ValueError as exc:
            raise _document_http_error(exc) from exc
        hits = [hit for hit in hits
                if str(hit.get('ref_id')) in tagged.get(str(hit.get('kind')), set())]
    return {"query": q, "scope": course or f"全部课程 · {workspace or '所有工作区'}", "count": len(hits), "hits": hits}


@router.get('/manual')
def platform_manual_search(q: str, limit: int = 8) -> dict:
    """查询随源码发布的平台使用手册，不混入课程知识图谱。"""
    hits = platform_manual.search(q, limit)
    return {'query': q, 'scope': 'CyberPal 平台', 'count': len(hits), 'hits': hits}


@router.get('/manual/open', response_class=HTMLResponse)
def platform_manual_open(path: str, line: int = 1) -> HTMLResponse:
    """Open one bundled manual at the cited Markdown line in a safe read-only view."""
    root = platform_manual.DOCS_DIR.resolve()
    requested = Path(path)
    if requested.is_absolute():
        raise HTTPException(400, '手册路径必须是相对路径')
    target = (root / requested).resolve()
    if not target.is_file() or target.suffix.lower() != '.md' or not target.is_relative_to(root):
        raise HTTPException(404, '手册章节不存在')
    try:
        lines = target.read_text(encoding='utf-8').splitlines()
    except OSError as exc:
        raise HTTPException(500, '手册读取失败') from exc
    focus = max(1, min(int(line or 1), max(len(lines), 1)))
    rows = []
    for number, value in enumerate(lines or [''], 1):
        cls = 'line focus' if number == focus else 'line'
        rows.append(
            f'<div id="line-{number}" class="{cls}"><span class="no">{number}</span>'
            f'<code>{html.escape(value)}</code></div>'
        )
    title = html.escape(target.stem)
    relative = html.escape(str(target.relative_to(root)))
    document = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>{title} · MinDock 手册</title>
<style>
body{{margin:0;background:#f6f3fb;color:#25212d;font:15px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}}
main{{max-width:1080px;margin:0 auto;padding:28px 34px 80px;background:#fff;min-height:100vh;box-sizing:border-box}}
h1{{font-size:22px;margin:0 0 4px;color:#4d2b78}} .meta{{color:#71677c;font-size:13px;margin-bottom:22px}}
.line{{display:grid;grid-template-columns:58px 1fr;border-left:3px solid transparent;min-height:26px}}
.line{{scroll-margin-top:18px}} .line.focus{{background:#f0e7fb;border-left-color:#7d4bb3;box-shadow:0 4px 14px rgba(83,45,125,.12)}}
.no{{color:#a69aae;text-align:right;padding:0 14px 0 0;user-select:none}} code{{white-space:pre-wrap;overflow-wrap:anywhere}}
a{{color:#6b3aa0}}
</style><main><h1>{title}</h1><div class="meta">MinDock 内置平台手册 · {relative} · 第 {focus} 行</div>
{''.join(rows)}<script>location.hash='line-{focus}';</script></main></html>'''
    return HTMLResponse(document)


@router.post('/search/rebuild')
def search_rebuild() -> dict:
    return {'ok': True, 'rows': search_index.rebuild()}


# ── 智能体问答（走真模型） ───────────────────────────
ASK_BUDGET_SECONDS = 240.0
_ASK_CANCEL = threading.local()
_ASK_STREAM_QUEUE = 256


def _ask_budget_seconds() -> float:
    """一次问答的墙钟预算。

    默认值给检索和 8 轮工具循环留出余量；运维或测试可以用
    MINDOCK_ASK_BUDGET_SECONDS 覆盖，非法值回退默认，避免把服务锁死。
    """
    raw = os.environ.get('MINDOCK_ASK_BUDGET_SECONDS', '')
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return ASK_BUDGET_SECONDS
    return value if value > 0 else ASK_BUDGET_SECONDS


class AskIn(BaseModel):
    question: str
    course: str | None = None       # None = 全局
    role_key: str = "laoba"
    tier: str | None = None         # 不传则按角色推断
    limit: int = 6
    history: list[dict] = []        # [{role, content}]，只取最近几轮
    web_mode: str = 'auto'          # auto / on / off
    workspace: str = 'studypal'
    mcp_server_id: str = 'auto'
    conversation_id: int | None = None  # 显式传入才在服务端落库

    @field_validator('mcp_server_id', mode='before')
    @classmethod
    def validate_mcp_selection(cls, value):
        from ..integrations.mcp_bridge import normalize_selection
        return normalize_selection(value)


@router.post("/agent/ask")
def agent_ask(body: AskIn) -> dict:
    return _agent_ask(body)


def _agent_ask(body: AskIn, progress=None) -> dict:
    """一次问答：先检索，再把记忆作为上下文交给模型，拿回带出处的回答。"""
    q = (body.question or "").strip()
    if not q:
        raise HTTPException(400, "问题不能为空")

    if body.web_mode not in ('auto', 'on', 'off'):
        raise HTTPException(400, 'web_mode 只能是 auto/on/off')
    conversation_id = body.conversation_id
    conversation = chat_repo.get_conversation(conversation_id) if conversation_id is not None else None
    if conversation_id is not None and conversation is None:
        raise HTTPException(400, '会话不存在')
    # 只有显式带 conversation_id 才落库；老客户端继续用 body.history 自己管会话。
    history = list(body.history or [])
    if conversation is not None and not history:
        history = [
            {'role': item.get('role'), 'content': item.get('content')}
            for item in (conversation.get('messages') or [])
            if item.get('role') in ('user', 'assistant') and item.get('content')
        ][-12:]
    deadline = time.monotonic() + _ask_budget_seconds()
    cancel_event = getattr(_ASK_CANCEL, 'event', None)
    should_cancel = cancel_event.is_set if cancel_event is not None else None
    hits = []
    agenda = None
    tier = body.tier or ('reasoning' if body.role_key == 'paper' else 'fast')
    workspace = repo.get_workspace(body.workspace or 'studypal')
    if not workspace:
        raise HTTPException(400, '工作区不存在')
    if body.course is not None and body.course not in {
        c['name'] for c in repo.list_courses(workspace['key'])
    }:
        raise HTTPException(400, '课程不属于当前工作区')
    scope_label = body.course or f"全部课程 · {workspace['name']}"
    web_results = []
    web_error = ''
    try:
        personal_context = ContextCompiler().build_context(
            workspace_key=body.workspace or 'studypal',
            course_key=body.course,
            question=q,
        )
    except Exception:
        # Context compilation is an enhancement. The normal tool loop remains
        # available if a stale local index or optional graph lookup is broken.
        personal_context = None
    started = time.monotonic()
    started_at = datetime.now().isoformat(timespec='seconds')
    conn = db.connect()
    run_id = conn.execute('INSERT INTO hub_runs(agent_key,started_at,status) VALUES(?,?,?)',
                          (body.role_key, started_at, 'running')).lastrowid
    conn.commit()
    conn.close()
    try:
        out = agent_chat.answer(
            q, hits, body.role_key, tier, scope_label, history, agenda=agenda,
            web_results=web_results, web_mode=body.web_mode, progress=progress,
            workspace=body.workspace or 'studypal', personal_context=personal_context,
            mcp_server_id=body.mcp_server_id,
            timeout_seconds=max(0.05, deadline - time.monotonic()),
            should_cancel=should_cancel,
        )
    except llm.LLMError as exc:
        # 超时/取消是一轮问答的正常结局，不该变成 500：返回同样的响应壳，
        # 让前端按 error_code 决定重试还是提示取消。
        out = {
            'ok': False, 'text': '', 'error': str(exc),
            'error_code': getattr(exc, 'code', '') or 'model_error',
            'hits': [], 'agenda': None, 'manual_results': [],
            'web_results': [], 'web_error': '', 'web_query': '',
            'trace': [], 'mcp_errors': [],
            'mcp_selection': {'requested': body.mcp_server_id, 'selected': [], 'status': 'unavailable'},
        }
    except Exception as exc:
        conn = db.connect()
        try:
            conn.execute("UPDATE hub_runs SET status='error',finished_at=?,duration_ms=?,error=? WHERE id=?",
                         (datetime.now().isoformat(timespec='seconds'), int((time.monotonic()-started)*1000), str(exc)[:500], run_id))
            conn.commit()
        finally:
            conn.close()
        raise
    conn = db.connect()
    try:
        tool_names = [str(item.get('tool')) for item in out.get('trace', []) if item.get('type') == 'tool']
        conn.execute("""UPDATE hub_runs SET status=?,finished_at=?,duration_ms=?,tool_summary=?,error=? WHERE id=?""",
                     ('success' if out.get('ok') else 'error', datetime.now().isoformat(timespec='seconds'),
                      int((time.monotonic()-started)*1000), ','.join(tool_names), str(out.get('error') or '')[:500], run_id))
        conn.commit()
    finally:
        conn.close()
    web_results = out.get('web_results', web_results)
    web_error = out.get('web_error', web_error)
    hits = out.get('hits', [])
    agenda = out.get('agenda')

    # 回执拆成两个布尔：前端按 user_saved 决定是否补写用户消息，避免
    # assistant 写入失败时把已落库的 user 消息再补写一遍。persisted 保持
    # 旧语义（user 与 assistant 都落库），老客户端无需改动。
    user_saved = False
    assistant_saved = False
    if conversation_id is not None:
        try:
            chat_repo.add_message(conversation_id, 'user', q,
                                  role_name=body.role_key, scope_label=scope_label)
            user_saved = True
        except Exception:
            user_saved = False
        if user_saved:
            try:
                chat_repo.add_message(conversation_id, 'assistant', out.get('text') or '',
                                      role_name=body.role_key, scope_label=scope_label,
                                      steps=out.get('trace', []),
                                      citations=out.get('hits', []),
                                      engine='model' if out.get('ok') else 'local',
                                      error=out.get('error') or '')
                assistant_saved = True
            except Exception:
                assistant_saved = False
            try:
                chat_repo.autotitle(conversation_id, q)
            except Exception:
                pass
    persisted = user_saved and assistant_saved

    return {
        "question": q,
        "scope": scope_label,
        "role_key": body.role_key,
        "tier": tier,
        "engine": "model" if out["ok"] else "local",
        "error": out["error"],
        "text": out["text"],
        "hits": hits,
        "count": len(hits),
        "agenda": agenda,
        "manual_results": out.get('manual_results', []),
        "model_ready": llm.available(tier),
        "web_results": web_results,
        "web_error": web_error,
        "web_query": out.get("web_query", ""),
        "trace": out.get('trace', []),
        "mcp_selection": out.get('mcp_selection', {'requested': body.mcp_server_id, 'selected': [], 'status': 'unavailable'}),
        "mcp_errors": out.get('mcp_errors', []),
        "error_code": out.get('error_code', ''),
        "conversation_id": conversation_id,
        "persisted": persisted,
        "user_saved": user_saved,
        "assistant_saved": assistant_saved,
    }


@router.post('/agent/ask/stream')
async def agent_ask_stream(body: AskIn, request: Request):
    if not body.question.strip() or body.web_mode not in ('auto', 'on', 'off'):
        raise HTTPException(400, '问题为空或联网模式无效')
    events: queue.Queue = queue.Queue(maxsize=_ASK_STREAM_QUEUE)
    cancel_event = threading.Event()

    def emit(item) -> bool:
        # 浏览器断开后生产者不能永远卡在满队列上，每步都检查取消标记。
        while not cancel_event.is_set():
            try:
                events.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def worker():
        # 通过 thread-local 把“客户端还在不在”传给 _agent_ask → chat → llm。
        _ASK_CANCEL.event = cancel_event
        try:
            emit({'type': 'result', 'data': _agent_ask(body, progress=emit)})
        except Exception:
            emit({'type': 'error', 'message': '问答执行失败，请重试并检查本地服务日志'})
        finally:
            _ASK_CANCEL.event = None
            emit(None)

    async def stream():
        threading.Thread(target=worker, daemon=True, name='mindock-ask-stream').start()
        last_beat = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    cancel_event.set()
                    break
                try:
                    item = events.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.25)
                    if time.monotonic() - last_beat >= 10:
                        last_beat = time.monotonic()
                        yield ': heartbeat\n\n'
                    continue
                if item is None:
                    break
                yield 'data: ' + json.dumps(item, ensure_ascii=False) + '\n\n'
        finally:
            # 无论正常结束还是客户端断开，都让后台工具循环尽快收尾。
            cancel_event.set()

    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.post("/web/save")
def web_save(body: WebSaveIn) -> dict:
    """保存一条联网资料到指定课程的 inbox，并立即建立本地索引。"""
    course = (body.course or "").strip()
    allowed = {c["name"] for c in repo.list_courses()}
    if not course or course not in allowed:
        raise HTTPException(400, "请选择有效课程")
    title = " ".join((body.title or "网页资料").split()).strip() or "网页资料"
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(400, "网页地址不能为空")
    try:
        web_search._public_url(url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    content = (body.body or "").strip()
    fetch_note = ""
    if not content:
        try:
            page = web_search.open_page(url)
            content = (page.get("body") or page.get("snippet") or "").strip()
            title = " ".join((page.get("title") or title).split()).strip() or title
        except Exception as exc:
            fetch_note = f"网页正文暂未抓取：{exc}"
    if not content:
        content = (body.snippet or "").strip()
    if not content:
        raise HTTPException(502, "网页没有可保存的摘要或正文")

    workspace_key = _course_workspace_key(course)
    folder = _workspace_folder(workspace_key) / _safe_filename(course) / "inbox"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = _safe_filename(f"网页资料-{title}")[:150] + f"-{stamp}.md"
    target = folder / filename
    markdown = (
        f"# {title}\n\n"
        f"- 来源：{url}\n"
        f"- 检索时间：{body.retrieved_at or datetime.now().isoformat(timespec='seconds')}\n"
        f"- 保存时间：{datetime.now().isoformat(timespec='seconds')}\n\n"
        f"## 摘要\n\n{body.snippet.strip() or content[:1500]}\n\n"
        f"## 正文摘录\n\n{content[:40000]}\n"
    )
    if fetch_note:
        markdown += f"\n> {fetch_note}\n"
    target.write_text(markdown, encoding="utf-8")
    scanned = sources.scan_course(course, [folder])
    file_status = next(
        (item for item in scanned["files"] if item["path"] == str(target.resolve())), None
    )
    return {
        "ok": True,
        "course": course,
        "filename": target.name,
        "folder": f"{workspace_key}/{_safe_filename(course)}/inbox",
        "status": "已保存并建立本地索引" if file_status and file_status.get("status") == "indexed" else "已保存，待索引",
        "index": file_status,
    }


@router.get("/agent/status")
def agent_status() -> dict:
    """模型层当前能不能用，以及库里存了多少对话。"""
    return {
        "tiers": {
            t: {"ready": llm.available(t), "configured": settings_store.configured().get(t, False)}
            for t in ("fast", "reasoning")
        },
        "chat": chat_repo.stats(),
    }


# ── 会话（对话落库，不再刷新即丢） ────────────────────
class ConvIn(BaseModel):
    title: str = "新对话"
    scope: str = "*"
    role_key: str = "laoba"
    tier: str = "fast"
    workspace: str = 'studypal'


@router.get("/conversations")
def conversations(limit: int = 50, workspace: str | None = None) -> list[dict]:
    return chat_repo.list_conversations(limit, workspace)


@router.post("/conversations")
def conversation_create(body: ConvIn) -> dict:
    try:
        return chat_repo.create_conversation(body.title, body.scope, body.role_key, body.tier, body.workspace)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/conversations/{conv_id}")
def conversation_get(conv_id: int) -> dict:
    d = chat_repo.get_conversation(conv_id)
    if not d:
        raise HTTPException(404, "会话不存在")
    return d


@router.patch("/conversations/{conv_id}")
def conversation_patch(conv_id: int, body: dict) -> dict:
    d = chat_repo.update_conversation(conv_id, body)
    if not d:
        raise HTTPException(404, "会话不存在")
    return d


@router.delete("/conversations/{conv_id}")
def conversation_delete(conv_id: int) -> dict:
    return {"deleted": chat_repo.delete_conversation(conv_id)}


class MsgIn(BaseModel):
    role: str                       # user / assistant
    content: str
    role_name: str = ""
    scope_label: str = ""
    steps: list = []
    citations: list = []
    writeback: list = []
    engine: str = "local"
    error: str = ""
    metadata: dict = {}


@router.post("/conversations/{conv_id}/messages")
def message_add(conv_id: int, body: MsgIn) -> dict:
    if not chat_repo.get_conversation(conv_id):
        raise HTTPException(404, "会话不存在")
    msg = chat_repo.add_message(
        conv_id, body.role, body.content,
        role_name=body.role_name, scope_label=body.scope_label,
        steps=body.steps, citations=body.citations, writeback=body.writeback,
        engine=body.engine, error=body.error, metadata=body.metadata,
    )
    if body.role == "user":
        chat_repo.autotitle(conv_id, body.content)
    return msg


# ── 知识文档 API（T2.21 / 026；知识页 T2.22 消费） ─────────────────────
def _document_http_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if '版本冲突' in message:
        return HTTPException(409, message)
    if '不存在' in message:
        return HTTPException(404, message)
    return HTTPException(400, message)


def _check_document_payload(fields: dict) -> None:
    body_md = fields.get('body_md')
    if body_md is not None and len(str(body_md)) > 200_000:
        raise HTTPException(400, '文档正文不能超过 200000 个字符')
    title = fields.get('title')
    if title is not None and len(str(title).strip()) > 200:
        raise HTTPException(400, '文档标题不能超过 200 个字符')
    tags = fields.get('tags')
    if tags is not None:
        if len(tags) > 20:
            raise HTTPException(400, '一篇文档最多 20 个标签')
        if any(len(str(tag)) > 40 for tag in tags):
            raise HTTPException(400, '单个标签不能超过 40 个字符')


def _course_filter_id(course) -> int | None:
    text = ' '.join(str(course or '').split()).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    conn = db.connect()
    try:
        row = conn.execute('SELECT id FROM courses WHERE name=?', (text,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(400, '课程不存在')
    return int(row['id'])


class DocumentIn(BaseModel):
    workspace_key: str = 'studypal'
    title: str
    body_md: str = ''
    kind: str = 'note'
    course_id: int | None = None
    kp_id: int | None = None
    tags: list[str] = []


class DocumentPatchIn(BaseModel):
    title: str | None = None
    body_md: str | None = None
    course_id: int | None = None
    kp_id: int | None = None
    kind: str | None = None
    status: str | None = None
    tags: list[str] | None = None
    expected_version: int | None = None


class KpCreateIn(BaseModel):
    course: str
    name: str
    gist: str = ''
    detail: str = ''
    trap: str = ''
    chapter: str = ''
    band: str = ''
    kp_type: str = ''
    source: str = ''


class TagIn(BaseModel):
    name: str
    workspace_key: str = 'studypal'
    workspace: str | None = None


class EntityTagIn(BaseModel):
    tag: str | int
    entity_type: Literal['document', 'kp', 'course']
    entity_id: int
    workspace_key: str | None = None
    workspace: str | None = None


@router.get('/documents')
def documents_list(workspace: str | None = None, course: str | None = None,
                   kind: str | None = None, status: str = 'active',
                   q: str | None = None, tag: str | None = None,
                   limit: int = 50, offset: int = 0) -> list[dict]:
    try:
        return documents.list(workspace=workspace, course=_course_filter_id(course),
                              kind=kind, status=status, q=q, tag=tag,
                              limit=limit, offset=offset)
    except ValueError as exc:
        raise _document_http_error(exc) from exc


@router.post('/documents')
def documents_create(body: DocumentIn) -> dict:
    _check_document_payload(body.model_dump())
    try:
        return documents.create(body.workspace_key, title=body.title, body_md=body.body_md,
                                kind=body.kind, course_id=body.course_id, kp_id=body.kp_id,
                                tags=body.tags, origin='api')
    except ValueError as exc:
        raise _document_http_error(exc) from exc


@router.get('/documents/lookup')
def documents_lookup(workspace_key: str = 'studypal', titles: str = '') -> dict:
    """Batch title -> document id for the Markdown preview wikilink resolver."""
    targets = [item.strip() for item in str(titles).split(',') if item.strip()]
    if not targets:
        return {'ids': {}, 'kp_ids': {}, 'missing': []}
    if len(targets) > 50:
        raise HTTPException(400, '一次最多查询 50 个标题')
    try:
        return wikilinks.lookup_titles(workspace_key, targets)
    except ValueError as exc:
        raise _document_http_error(exc) from exc


@router.get('/documents/{document_id}')
def documents_detail(document_id: int) -> dict:
    document = documents.get(document_id)
    if not document or document['status'] == 'deleted':
        raise HTTPException(404, '文档不存在')
    try:
        revisions = documents.list_revisions(document_id)
        links = wikilinks.backlinks(document)
    except ValueError as exc:
        raise _document_http_error(exc) from exc
    return {'document': document, 'tags': document.get('tags', []),
            'backlinks': links['backlinks'], 'bad_links': links['bad_links'],
            'revisions_count': len(revisions)}


@router.patch('/documents/{document_id}')
def documents_patch(document_id: int, body: DocumentPatchIn) -> dict:
    fields = body.model_dump(exclude_unset=True)
    _check_document_payload(fields)
    expected_version = fields.pop('expected_version', None)
    try:
        return documents.update(document_id, expected_version=expected_version,
                                origin='api', **fields)
    except ValueError as exc:
        raise _document_http_error(exc) from exc


@router.delete('/documents/{document_id}')
def documents_delete(document_id: int, expected_version: int | None = None) -> dict:
    try:
        document = documents.soft_delete(document_id, expected_version=expected_version)
    except ValueError as exc:
        raise _document_http_error(exc) from exc
    return {'ok': True, 'document': document}


@router.post('/documents/{document_id}/restore')
def documents_restore(document_id: int) -> dict:
    try:
        return {'ok': True, 'document': documents.restore(document_id)}
    except ValueError as exc:
        raise _document_http_error(exc) from exc


@router.get('/documents/{document_id}/revisions')
def documents_revisions(document_id: int) -> list[dict]:
    try:
        return documents.list_revisions(document_id)
    except ValueError as exc:
        raise _document_http_error(exc) from exc


@router.post('/documents/{document_id}/revisions/{version}/restore')
def documents_restore_revision(document_id: int, version: int) -> dict:
    try:
        return {'ok': True, 'document': documents.restore_revision(document_id, version)}
    except ValueError as exc:
        raise _document_http_error(exc) from exc


@router.get('/documents/{document_id}/backlinks')
def documents_backlinks(document_id: int) -> dict:
    document = documents.get(document_id)
    if not document or document['status'] == 'deleted':
        raise HTTPException(404, '文档不存在')
    try:
        return wikilinks.backlinks(document)
    except ValueError as exc:
        raise _document_http_error(exc) from exc


@router.get('/tags')
def tags_list(workspace_key: str | None = None,
              workspace: str | None = None) -> list[dict]:
    """Tags of a workspace with live usage counts (document/kp/course)."""
    try:
        return documents.list_tags(workspace or workspace_key)
    except ValueError as exc:
        raise _document_http_error(exc) from exc


@router.post('/tags')
def tags_create(body: TagIn) -> dict:
    try:
        tag = documents.ensure_tag(body.workspace or body.workspace_key, body.name)
    except ValueError as exc:
        raise _document_http_error(exc) from exc
    return {'ok': True, 'tag': tag}


@router.delete('/tags/{tag_id}')
def tags_delete(tag_id: int) -> dict:
    try:
        return {'ok': True, 'deleted': documents.delete_tag(tag_id)}
    except ValueError as exc:
        raise _document_http_error(exc) from exc


@router.get('/tags/{tag_id}/entities')
def tags_entities(tag_id: int) -> dict:
    try:
        return documents.entities_of_tag(tag_id)
    except ValueError as exc:
        raise _document_http_error(exc) from exc


@router.post('/entity-tags')
def entity_tags_attach(body: EntityTagIn) -> dict:
    try:
        link = documents.attach_entity(body.tag, body.entity_type, body.entity_id,
                                       workspace_key=body.workspace or body.workspace_key)
    except ValueError as exc:
        raise _document_http_error(exc) from exc
    return {'ok': True, 'link': link}


@router.delete('/entity-tags')
def entity_tags_detach(tag: str, entity_type: str, entity_id: int,
                       workspace: str | None = None,
                       workspace_key: str | None = None) -> dict:
    try:
        link = documents.detach_entity(tag, entity_type, entity_id,
                                       workspace_key=workspace or workspace_key)
    except ValueError as exc:
        raise _document_http_error(exc) from exc
    return {'ok': True, 'link': link}


@router.post('/kp')
def kp_create(body: KpCreateIn) -> dict:
    try:
        kp = repo.create_kp(body.course, body.name, gist=body.gist, detail=body.detail,
                            trap=body.trap, chapter=body.chapter, band=body.band,
                            kp_type=body.kp_type, source=body.source)
    except ValueError as exc:
        raise _document_http_error(exc) from exc
    return {'ok': True, 'kp': kp}


@router.delete('/kp/{kp_id}')
def kp_delete(kp_id: int) -> dict:
    try:
        return repo.soft_delete_kp(kp_id)
    except ValueError as exc:
        raise _document_http_error(exc) from exc


# --- B3 T3.7 学习事件（028 三表，append-only；写入口 app/memory/learning.py）---


class LearningEventIn(BaseModel):
    kind: str
    workspace_key: str | None = None
    course: str | None = None
    kp_id: int | None = None
    document_id: int | None = None
    source_id: int | None = None
    payload: dict | None = None
    duration_seconds: int = 0
    occurred_at: str = ''
    idempotency_key: str = ''


class LearningSessionIn(BaseModel):
    kind: str = 'study'
    workspace_key: str | None = None
    course: str | None = None
    source: str = 'manual'
    idempotency_key: str = ''


class LearningSessionPatchIn(BaseModel):
    ended_at: str | None = None


_LEARNING_NOT_FOUND = ('学习会话不存在', '知识点不存在', '文档不存在', '材料不存在')


def _learning_http_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if message in _LEARNING_NOT_FOUND:
        return HTTPException(404, message)
    return HTTPException(400, message)


@router.post('/learning/events')
def learning_event_create(body: LearningEventIn) -> dict:
    try:
        event, duplicated = learning.record_event(
            body.kind,
            workspace_key=body.workspace_key,
            course=body.course,
            kp_id=body.kp_id,
            document_id=body.document_id,
            source_id=body.source_id,
            payload=body.payload,
            duration_seconds=body.duration_seconds,
            occurred_at=body.occurred_at,
            idempotency_key=body.idempotency_key,
        )
    except ValueError as exc:
        raise _learning_http_error(exc) from exc
    return {'ok': True, 'event': event, 'duplicated': duplicated}


@router.post('/learning/sessions')
def learning_session_create(body: LearningSessionIn) -> dict:
    try:
        session, duplicated = learning.start_session(
            body.kind,
            workspace_key=body.workspace_key,
            course=body.course,
            source=body.source,
            idempotency_key=body.idempotency_key,
        )
    except ValueError as exc:
        raise _learning_http_error(exc) from exc
    return {'ok': True, 'session': session, 'duplicated': duplicated}


@router.patch('/learning/sessions/{session_id}')
def learning_session_update(session_id: int, body: LearningSessionPatchIn) -> dict:
    try:
        session = learning.end_session(session_id, ended_at=body.ended_at or '')
    except ValueError as exc:
        raise _learning_http_error(exc) from exc
    return {'ok': True, 'session': session}


@router.get('/learning/summary')
def learning_summary(range: str = 'today', workspace_key: str | None = None) -> dict:
    try:
        return learning.summary(range, workspace_key=workspace_key)
    except ValueError as exc:
        raise _learning_http_error(exc) from exc


@router.get('/learning/timeline')
def learning_timeline(course: str | None = None, limit: int = 50,
                      workspace_key: str | None = None) -> list[dict]:
    try:
        return learning.timeline(course=course, limit=limit, workspace_key=workspace_key)
    except ValueError as exc:
        raise _learning_http_error(exc) from exc


# --- B3 T3.9 复习队列 v0（固定规则 + 三档反馈，不新增表）---


class ReviewFeedbackIn(BaseModel):
    kp_id: int
    result: Literal['again', 'hard', 'good']
    workspace: str | None = None


@router.get('/review/due')
def review_due(workspace: str | None = None, course: str | None = None,
               limit: int = 20) -> dict:
    try:
        return review_queue.due(workspace=workspace or None, course=course or None, limit=limit)
    except review_queue.ReviewError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/review/feedback')
def review_feedback(body: ReviewFeedbackIn) -> dict:
    try:
        return review_queue.feedback(body.kp_id, body.result, workspace=body.workspace or None)
    except review_queue.ReviewError as exc:
        status = 404 if '不存在' in str(exc) else 400
        raise HTTPException(status, str(exc)) from exc


# --- B3 T3.11 Markdown 导出 v0（vault 目录 + frontmatter + manifest，单向）---


class KnowledgeExportIn(BaseModel):
    workspace_key: str = 'studypal'
    dest_dir: str | None = None


@router.post('/knowledge/export')
def knowledge_export(body: KnowledgeExportIn) -> dict:
    from ..memory import exporter
    try:
        return exporter.export_knowledge(body.workspace_key, dest_dir=body.dest_dir)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# --- B3 T3.5 材料原文/分块读取（O-LEARN-1）---


def _material_http_error(exc: ValueError) -> HTTPException:
    from ..memory import materials
    if isinstance(exc, materials.MaterialNotFound):
        return HTTPException(404, str(exc))
    return HTTPException(400, str(exc))


@router.get('/materials/{source_id}')
def material_source(source_id: int) -> dict:
    from ..memory import materials
    try:
        source = materials.resolve_source(source_id)
    except materials.MaterialError as exc:
        raise _material_http_error(exc) from exc
    total = source.pop('total_evidence')
    return {'source': source, 'total_evidence': total}


@router.get('/materials/{source_id}/blocks')
def material_blocks(source_id: int, from_locator: str | None = None,
                    limit: int = 50, locator_type: str | None = None) -> dict:
    from ..memory import materials
    try:
        return materials.blocks(source_id, from_locator=from_locator,
                                limit=limit, locator_type=locator_type)
    except materials.MaterialError as exc:
        raise _material_http_error(exc) from exc


@router.get('/materials/{source_id}/page.png')
def material_page(source_id: int, locator: str | None = None,
                  dpi: int = 144) -> Response:
    from ..memory import materials
    try:
        png = materials.render_page_png(source_id, locator=locator, dpi=dpi)
    except materials.UnsupportedPageRender as exc:
        body = json.dumps({'detail': str(exc), 'fallback': exc.fallback},
                          ensure_ascii=False)
        return Response(content=body.encode('utf-8'), status_code=415,
                        media_type='application/json',
                        headers={'Cache-Control': 'no-store'})
    except materials.MaterialError as exc:
        raise _material_http_error(exc) from exc
    return Response(content=png, media_type='image/png',
                    headers={'Cache-Control': 'no-store'})


@router.get('/evidence/{evidence_id}/context')
def evidence_context(evidence_id: int, before: int = 1, after: int = 1) -> dict:
    from ..memory import materials
    try:
        return materials.evidence_context(evidence_id, before=before, after=after)
    except materials.MaterialError as exc:
        raise _material_http_error(exc) from exc

# --- 0.6.2 旁听转录 v0（029 两表；引擎 app/memory/asr.py，编排 app/memory/lecture.py）---


class LectureIn(BaseModel):
    workspace_key: str | None = None
    course: str | None = None
    title: str = ''
    source: str = 'mic'
    model: str | None = None
    idempotency_key: str = ''


class LectureFinalizeIn(BaseModel):
    title: str = ''
    make_document: bool = True


def _lecture_http_error(exc: ValueError) -> HTTPException:
    if isinstance(exc, lecture.LectureNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, lecture.EngineUnavailable):
        return HTTPException(503, str(exc))
    if isinstance(exc, (lecture.ModelMissing, lecture.DownloadBusy, lecture.ChunkConflict)):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


@router.get('/asr/status')
def asr_status() -> dict:
    return lecture.status()


@router.post('/asr/models/{model}/download')
def asr_model_download(model: str) -> dict:
    try:
        return lecture.start_download(model)
    except ValueError as exc:
        raise _lecture_http_error(exc) from exc


@router.post('/lectures')
def lectures_create(body: LectureIn) -> dict:
    try:
        return lecture.create_session(
            body.workspace_key, course=body.course, title=body.title,
            source=body.source, model=body.model,
            idempotency_key=body.idempotency_key)
    except ValueError as exc:
        raise _lecture_http_error(exc) from exc


@router.get('/lectures')
def lectures_list(workspace_key: str | None = None, course: str | None = None,
                  limit: int = 20) -> dict:
    try:
        return lecture.list_sessions(workspace_key, course=course, limit=limit)
    except ValueError as exc:
        raise _lecture_http_error(exc) from exc


@router.get('/lectures/{session_id}')
def lectures_get(session_id: int) -> dict:
    try:
        return lecture.get_session(session_id)
    except ValueError as exc:
        raise _lecture_http_error(exc) from exc


@router.post('/lectures/{session_id}/chunks')
async def lectures_chunks(session_id: int, request: Request) -> dict:
    pcm = await request.body()
    try:
        return await run_in_threadpool(
            lecture.feed, session_id, pcm,
            chunk_id=request.headers.get('X-Lecture-Chunk-Id', ''))
    except lecture.ChunkAcceptedError as exc:
        return Response(
            content=json.dumps({'detail': str(exc), 'accepted': True,
                                'chunk_id': exc.chunk_id}, ensure_ascii=False),
            status_code=503, media_type='application/json',
            headers={'X-Lecture-Chunk-Accepted': '1', 'X-Lecture-Chunk-Id': exc.chunk_id})
    except ValueError as exc:
        raise _lecture_http_error(exc) from exc
    except asr.AsrUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post('/lectures/{session_id}/finalize')
def lectures_finalize(session_id: int, body: LectureFinalizeIn) -> dict:
    try:
        return lecture.finalize(session_id, title=body.title,
                                make_document=body.make_document)
    except ValueError as exc:
        raise _lecture_http_error(exc) from exc


@router.delete('/lectures/{session_id}')
def lectures_discard(session_id: int) -> dict:
    try:
        return lecture.discard(session_id)
    except ValueError as exc:
        raise _lecture_http_error(exc) from exc


# --- WorkPal 一键导入 Obsidian Wiki（模块 app/memory/wiki_import.py）---------------


class WikiImportIn(BaseModel):
    vault_root: str
    course: str = 'AI工作系统'


def _wiki_import_course(value: str) -> str:
    course = (value or '').strip()
    if not course or len(course) > 80 or '/' in course or '\\' in course:
        raise HTTPException(400, '课程名称不能为空、不能超过 80 个字符或包含路径分隔符')
    return course


@router.get('/wiki/import/status')
def wiki_import_status() -> dict:
    state = wiki_import.status()
    state['candidates'] = wiki_import.vault_candidates()
    return state


@router.post('/wiki/import', status_code=202)
def wiki_import_start(body: WikiImportIn) -> dict:
    try:
        root = wiki_import.resolve_wiki_root(body.vault_root)
        course = wiki_import._check_course(body.course)
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if wiki_import.status()['state'] == 'running':
        raise HTTPException(409, '已有 Wiki 导入任务正在运行')
    return {'ok': True, **wiki_import.start_import(root, course)}


@router.post('/wiki/import/preview')
def wiki_import_preview(body: WikiImportIn) -> dict:
    try:
        return wiki_import.import_pages(body.vault_root, body.course, apply=False)
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
