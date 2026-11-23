"""O-REL-3: a desktop shell loads only the backend instance it launched."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAC_SHELL = ROOT / 'desktop/MinDock.swift'
WINDOWS_SHELL = ROOT / 'desktop/windows/runtime.cjs'
WINDOWS_TESTS = ROOT / 'desktop/windows/tests/runtime.test.cjs'


def _patch_health(monkeypatch):
    from app import main
    monkeypatch.setattr(main, 'table_counts', lambda: {'courses': 0})
    return main


def test_health_echoes_ready_nonce(monkeypatch):
    main = _patch_health(monkeypatch)
    monkeypatch.setenv('MINDOCK_READY_NONCE', 'launch-nonce-42')
    assert main.health()['nonce'] == 'launch-nonce-42'


def test_health_nonce_defaults_to_empty_for_compatibility(monkeypatch):
    main = _patch_health(monkeypatch)
    monkeypatch.delenv('MINDOCK_READY_NONCE', raising=False)
    assert main.health()['nonce'] == ''


def test_macos_shell_passes_and_verifies_the_nonce():
    source = MAC_SHELL.read_text(encoding='utf-8')
    assert 'private let readyNonce = UUID().uuidString' in source
    assert 'env["MINDOCK_READY_NONCE"] = readyNonce' in source
    nonce_check = 'guard (payload?["nonce"] as? String) == self.readyNonce else {'
    assert nonce_check in source
    assert '本地服务校验失败' in source
    load_page = 'self.web.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))'
    assert load_page in source
    # The identity check must run before any page is loaded from the port.
    assert source.index(nonce_check) < source.index(load_page)


def test_windows_shell_passes_and_verifies_the_nonce():
    source = WINDOWS_SHELL.read_text(encoding='utf-8')
    assert "this.readyNonce = crypto.randomBytes(16).toString('hex')" in source
    assert 'this.env.MINDOCK_READY_NONCE = this.readyNonce' in source
    assert 'probeHealth(base,this.token,this.readyNonce)' in source
    assert 'probe.nonce !== this.readyNonce' in source
    assert 'ready nonce mismatch' in source
    assert 'module.exports={Runtime,healthy,probeHealth}' in source


def test_windows_shell_rejects_a_foreign_service_with_another_nonce():
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is unavailable in this environment')
    assert WINDOWS_TESTS.exists()
    completed = subprocess.run(
        [node, '--test', str(WINDOWS_TESTS)], cwd=str(ROOT),
        capture_output=True, text=True, timeout=180)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert 'ready nonce' in output
