"""MCP management stays local, including when development CORS is permissive."""
import ipaddress
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.integrations import mcp_bridge as bridge, mcp_settings as settings
from app.memory import repo


_SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})
_LOOPBACK_ERROR = 'MCP 配置仅允许本机访问'
_ORIGIN_ERROR = 'MCP 配置仅允许同源页面访问'


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return host == 'localhost'


def _check_local_request(request: Request, *, safe_methods: bool) -> None:
    # Read-only requests stay available for the embedded page and shell health
    # probes; desktop mode still protects them with its per-process token.
    if safe_methods and request.method in _SAFE_METHODS:
        return
    if (not request.client or not _loopback(request.client.host)
            or not _loopback(request.url.hostname or '')):
        raise HTTPException(403, _LOOPBACK_ERROR)
    origin = request.headers.get('origin')
    expected = f'{request.url.scheme}://{request.url.netloc}'
    if (origin is not None and origin != expected) or request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, _ORIGIN_ERROR)


def local_only(request: Request):
    _check_local_request(request, safe_methods=False)


def verify_local_request(request: Request) -> None:
    """Reusable local-only guard for state-changing local API calls.

    Mounted by app.main on every router under /api so a page served from
    another origin (or a remote client) cannot POST/PUT/DELETE local state.
    """
    _check_local_request(request, safe_methods=True)


router = APIRouter(prefix='/extensions/mcp', dependencies=[Depends(local_only)])


async def body(request):
    if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
        raise HTTPException(415, '请使用 JSON 配置')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > bridge.MAX_CONFIG:
            raise HTTPException(413, 'MCP 请求超出大小限制')
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, 'MCP 请求格式无效') from None


def safe_call(fn, *args):
    try:
        return fn(*args)
    except settings.RevisionConflict:
        raise HTTPException(409, '配置已变更，请重新加载后再保存') from None
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        # Validation errors must never reflect arbitrary executable arguments.
        raise HTTPException(422, 'MCP 配置无效；请检查路径、工作区和工具授权，禁止填写明文密钥') from None


@router.get('')
def get_config():
    return safe_call(settings.read)


@router.put('')
async def put_config(request: Request):
    value = await body(request)
    return await run_in_threadpool(safe_call, settings.save, value.get('servers'), value.get('revision'))


def _probe(server):
    settings.validate([server])
    if server.get('trusted') is not True:
        raise ValueError('必须先信任服务')
    settings._check_path()
    # SDK subprocess cwd needs an existing private directory; no config saved.
    settings.config_path().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return bridge.inspect_server(server)


@router.post('/probe')
async def probe(request: Request):
    value = await body(request)
    return await run_in_threadpool(safe_call, _probe, value.get('server'))


def _test(value):
    config = settings.read()
    selected = next((s for s in config['servers'] if s['id'] == value.get('id')), None)
    if selected is None:
        raise ValueError()
    workspace, course = value.get('workspace'), value.get('course')
    web_mode = value.get('web_mode', 'auto')
    if web_mode not in ('auto', 'off', 'on') or not isinstance(workspace, str):
        raise ValueError()
    if not repo.get_workspace(workspace) or (course is not None and (
            not isinstance(course, str) or course not in {c['name'] for c in repo.list_courses(workspace)})):
        raise ValueError()
    tools = bridge.discover(workspace, course, web_mode, server_id=selected['id'])
    name = f"mcp_{selected['id']}_{value.get('tool', '')}"
    result = tools.call(name, value.get('arguments', {}))
    return {'ok': 'error' not in result, 'result': result,
            'mcp_selection': tools.selection, 'mcp_errors': tools.errors}


@router.post('/test')
async def test_tool(request: Request):
    return await run_in_threadpool(safe_call, _test, await body(request))
