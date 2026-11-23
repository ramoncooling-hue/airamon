from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_frontend_resources_revalidate_after_application_upgrade(tmp_path):
    from app.static import SECURITY_HEADERS, RevalidatingStaticFiles

    (tmp_path / 'index.html').write_text('<h1>Current app</h1>')
    (tmp_path / 'app.js').write_text('window.version = 2;')
    app = FastAPI()
    app.mount('/', RevalidatingStaticFiles(directory=tmp_path, html=True))
    with TestClient(app) as client:
        for path in ['/', '/index.html', '/app.js']:
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-cache, must-revalidate'
            for name, value in SECURITY_HEADERS.items():
                assert response.headers[name.lower()] == value
            cached = client.get(path, headers={'If-None-Match': response.headers['etag']})
            assert cached.status_code == 304
            assert cached.headers['cache-control'] == 'no-cache, must-revalidate'
            for name, value in SECURITY_HEADERS.items():
                assert cached.headers[name.lower()] == value


def test_security_headers_are_scoped_and_block_external_content():
    from app import static

    headers = static.SECURITY_HEADERS
    csp = headers['Content-Security-Policy']
    # 前端仍是单文件内联脚本 + 内联样式，CSP 必须放行同源/内联，同时禁止外部与 plugin 内容。
    assert "default-src 'self'" in csp
    assert "img-src 'self' data:" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert "script-src 'self' 'unsafe-inline'" in csp
    # 本机单页应用不允许被 iframe 嵌套、改写 base URI、加载插件对象或向外部提交表单。
    assert "object-src 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'self'" in csp
    assert headers['X-Content-Type-Options'] == 'nosniff'
    assert headers['Referrer-Policy'] == 'no-referrer'


def test_security_headers_do_not_override_explicit_response_headers(tmp_path):
    from app.static import RevalidatingStaticFiles

    (tmp_path / 'asset.txt').write_text('hello')
    app = FastAPI()
    app.mount('/', RevalidatingStaticFiles(directory=tmp_path, html=True))
    with TestClient(app) as client:
        response = client.get('/asset.txt')
        assert response.headers['content-type'].startswith('text/plain')
        assert response.headers['x-content-type-options'] == 'nosniff'


def test_native_launch_and_refresh_bypass_old_html_cache():
    source = (Path(__file__).resolve().parents[1] / 'desktop/MinDock.swift').read_text()
    assert 'cachePolicy: .reloadIgnoringLocalCacheData' in source
    assert 'web?.reloadFromOrigin()' in source
