"""O-SEC-1: a remote or cross-origin page must not change local state."""

from __future__ import annotations

import logging
import os

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.mcp_routes import _LOOPBACK_ERROR, _ORIGIN_ERROR, verify_local_request

LOCAL_BASE = 'http://127.0.0.1:8765'
LOCAL_CLIENT = ('127.0.0.1', 43210)


def _local_client(app, base_url=LOCAL_BASE, client=LOCAL_CLIENT):
    return TestClient(app, base_url=base_url, client=client)


def _dependency_calls(dependant):
    for dependency in dependant.dependencies:
        yield dependency.call
        yield from _dependency_calls(dependency)


def _guarded_app():
    app = FastAPI()
    api = APIRouter()

    @api.get('/read')
    def read():
        return {'ok': True}

    @api.post('/write')
    def write():
        return {'ok': True}

    app.include_router(api, dependencies=[Depends(verify_local_request)])
    return app


def test_cross_origin_post_is_forbidden():
    response = _local_client(_guarded_app()).post(
        '/write', headers={'origin': 'https://evil.example'})
    assert response.status_code == 403
    assert response.json()['detail'] == _ORIGIN_ERROR


def test_cross_site_fetch_metadata_is_forbidden():
    response = _local_client(_guarded_app()).post(
        '/write', headers={'sec-fetch-site': 'cross-site'})
    assert response.status_code == 403
    assert response.json()['detail'] == _ORIGIN_ERROR


def test_same_origin_post_passes():
    response = _local_client(_guarded_app()).post(
        '/write', headers={'origin': LOCAL_BASE})
    assert response.status_code == 200
    assert response.json() == {'ok': True}


def test_local_post_without_origin_passes():
    response = _local_client(_guarded_app()).post('/write')
    assert response.status_code == 200
    assert response.json() == {'ok': True}


def test_cross_origin_form_post_without_origin_is_allowed_by_design():
    """B1.1/P2-5 现状固化：老浏览器/非浏览器表单不发送 Origin。

    本机桌面设计取舍：守卫只校验 client.host 与 Host 都是 loopback，因此
    无 Origin 的跨源表单式 POST 当前会放行；安全边界要求服务只 bind
    loopback、不做公网/反向代理部署（见交接单「安全边界」）。
    """
    response = _local_client(_guarded_app()).post(
        '/write',
        headers={'content-type': 'application/x-www-form-urlencoded'},
        content='field=value',
    )
    assert response.status_code == 200
    assert response.json() == {'ok': True}


def test_remote_client_is_forbidden():
    client = TestClient(_guarded_app(), base_url=LOCAL_BASE,
                        client=('10.20.30.40', 5000))
    response = client.post('/write')
    assert response.status_code == 403
    assert response.json()['detail'] == _LOOPBACK_ERROR


def test_non_loopback_host_is_forbidden():
    client = TestClient(_guarded_app(), base_url='http://192.168.1.20:8765',
                        client=LOCAL_CLIENT)
    response = client.post('/write')
    assert response.status_code == 403
    assert response.json()['detail'] == _LOOPBACK_ERROR


def test_get_requests_are_not_blocked():
    client = TestClient(_guarded_app(), base_url='http://192.168.1.20:8765',
                        client=('10.20.30.40', 5000))
    response = client.get('/read')
    assert response.status_code == 200
    assert response.json() == {'ok': True}


def _included_api_router(main):
    """FastAPI keeps include-time dependencies on the included-router wrapper."""
    for route in main.app.routes:
        if type(route).__name__ == '_IncludedRouter':
            paths = {getattr(r, 'path', '') for r in route.original_router.routes}
            if '/api/settings' in paths:
                return route
    raise AssertionError('app.main does not include the /api router')


def test_main_mounts_the_guard_on_state_changing_api_routes():
    from fastapi.routing import APIRoute
    from app import main
    included = _included_api_router(main)
    dependencies = getattr(included.include_context, 'dependencies', [])
    assert verify_local_request in {dep.dependency for dep in dependencies}
    health = next(r for r in main.app.routes if isinstance(r, APIRoute)
                  and r.path == '/api/health')
    assert verify_local_request not in set(_dependency_calls(health.dependant))


def test_real_api_rejects_cross_origin_settings_writes(monkeypatch):
    from app import main
    if os.environ.get('MINDOCK_SESSION_TOKEN'):
        pytest.skip('desktop-mode app routes every request through the session guard')
    client = _local_client(main.app)
    headers = {'origin': 'https://evil.example'}
    put = client.put('/api/settings', headers=headers, json={})
    assert put.status_code == 403
    assert put.json()['detail'] == _ORIGIN_ERROR
    probe = client.post('/api/settings/test', headers=headers, json={})
    assert probe.status_code == 403
    assert probe.json()['detail'] == _ORIGIN_ERROR


def test_real_health_get_is_available_to_non_loopback_test_clients(monkeypatch):
    from app import main
    monkeypatch.setattr(main, 'table_counts', lambda: {'courses': 0})
    # The default test client is not loopback; GET must stay unaffected.
    response = TestClient(main.app).get('/api/health')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'


def _cors_app():
    app = FastAPI()

    @app.get('/api/ping')
    def ping():
        return {'ok': True}

    return app


def test_cors_allows_only_loopback_origins_by_default():
    from app import main
    app = _cors_app()
    main.configure_cors(app, environ={})
    client = TestClient(app)
    allowed = client.options('/api/ping', headers={
        'origin': 'http://localhost:5173',
        'access-control-request-method': 'GET'})
    assert allowed.headers.get('access-control-allow-origin') == 'http://localhost:5173'
    blocked = client.options('/api/ping', headers={
        'origin': 'https://evil.example',
        'access-control-request-method': 'GET'})
    assert 'access-control-allow-origin' not in blocked.headers


def test_dev_cors_opt_in_warns_and_opens(caplog):
    from app import main
    app = _cors_app()
    with caplog.at_level(logging.WARNING):
        main.configure_cors(app, environ={'MINDOCK_DEV_CORS': '1'})
    assert 'MINDOCK_DEV_CORS=1' in caplog.text
    response = TestClient(app).options('/api/ping', headers={
        'origin': 'https://evil.example',
        'access-control-request-method': 'GET'})
    assert response.headers.get('access-control-allow-origin') == '*'


def test_mcp_router_keeps_strict_local_only_for_gets():
    from app.api import mcp_routes
    app = FastAPI()
    app.include_router(mcp_routes.router)
    response = TestClient(app, client=('10.0.0.5', 9999)).get('/extensions/mcp')
    assert response.status_code == 403
    assert response.json()['detail'] == _LOOPBACK_ERROR
