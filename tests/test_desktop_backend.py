import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
import pytest

from app import desktop
from app.maintenance import lock
from desktop import backend


def test_explicit_parent_pid_and_legacy_posix_parent():
    with patch.dict(os.environ, {'MINDOCK_PARENT_PID': ''}), patch.object(desktop.os, 'getppid', return_value=4321):
        assert desktop.get_parent_pid() == 4321
    with patch.dict(os.environ, {'MINDOCK_PARENT_PID': '8765'}):
        assert desktop.get_parent_pid() == 8765
    for value in ('not-a-pid', '0', '-1'):
        with patch.dict(os.environ, {'MINDOCK_PARENT_PID': value}), pytest.raises(ValueError):
            desktop.get_parent_pid()
    with patch.object(desktop.sys, 'platform', 'darwin'), \
            patch.object(desktop.os, 'getppid', side_effect=[4321, 1]):
        assert desktop.parent_is_alive(4321)
        assert not desktop.parent_is_alive(4321)


@pytest.mark.parametrize('wait_result,alive', [(258, True), (0, False)])
def test_windows_parent_uses_process_handle_even_when_getppid_is_unchanged(wait_result, alive):
    kernel = Mock()
    handle = 2 ** 40
    kernel.OpenProcess.return_value = handle
    kernel.WaitForSingleObject.return_value = wait_result
    with patch.object(desktop.sys, 'platform', 'win32'), \
            patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
            patch.object(desktop.os, 'getppid', side_effect=AssertionError('Windows PID does not reparent')):
        assert desktop.parent_is_alive(8765) is alive
    kernel.OpenProcess.assert_called_once_with(0x00100000, False, 8765)
    assert kernel.OpenProcess.restype is wintypes.HANDLE
    kernel.WaitForSingleObject.assert_called_once_with(handle, 0)
    kernel.CloseHandle.assert_called_once_with(handle)


@pytest.mark.parametrize('error,alive', [(87, False), (5, True)])
def test_windows_parent_open_failure_distinguishes_absent_from_access_denied(error, alive):
    kernel = Mock()
    kernel.OpenProcess.return_value = 0
    with patch.object(desktop.sys, 'platform', 'win32'), \
            patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
            patch.object(ctypes, 'get_last_error', return_value=error, create=True):
        assert desktop.parent_is_alive(8765) is alive
    kernel.CloseHandle.assert_not_called()


def test_windows_parent_wait_failure_closes_handle():
    kernel = Mock()
    kernel.OpenProcess.return_value = 123
    kernel.WaitForSingleObject.return_value = 0xFFFFFFFF
    with patch.object(desktop.sys, 'platform', 'win32'), \
            patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
            patch.object(ctypes, 'get_last_error', return_value=6, create=True), \
            pytest.raises(OSError):
        desktop.parent_is_alive(8765)
    kernel.CloseHandle.assert_called_once_with(123)


@pytest.mark.parametrize('alive', [False, True])
def test_maintenance_requires_matching_explicit_live_parent(tmp_path, alive):
    with patch.dict(os.environ, {'MINDOCK_PARENT_PID': '8765', 'MINDOCK_MAINTENANCE_OWNER_PID': '8765'}), \
            patch.object(desktop, 'parent_is_alive', return_value=alive):
        if alive:
            with lock.check_legacy_desktop(tmp_path, allow_desktop_lock=True):
                pass
        else:
            with pytest.raises(lock.RuntimeBusyError):
                with lock.check_legacy_desktop(tmp_path, allow_desktop_lock=True):
                    pytest.fail('Orphan maintenance was permitted')


@pytest.mark.parametrize('reason', ['parent', 'file', 'finished'])
def test_watcher_exits_for_parent_loss_file_signal_or_finished_work(tmp_path, reason):
    signal = tmp_path / 'shutdown'
    if reason == 'file':
        signal.touch()
    stopped = Mock()
    stopped.wait.side_effect = [reason == 'finished', True]
    on_stop = Mock()
    with patch.object(backend, 'parent_is_alive', return_value=reason != 'parent'):
        backend._watch_parent(8765, stopped, on_stop, signal)
    assert on_stop.call_count == (0 if reason == 'finished' else 1)


@pytest.mark.parametrize('fail', [False, True])
def test_serve_preserves_startup_signal_gracefully_stops_and_cleans_files(tmp_path, fail):
    root = desktop.initialize_data_dir(tmp_path / 'runtime')
    private = tmp_path / 'private-run'
    private.mkdir()
    signal, descriptor = private / 'shutdown', private / 'session.json'
    signal.touch()
    server = SimpleNamespace(should_exit=False, started=True)

    def run(*, sockets):
        assert signal.exists()
        assert sockets[0].getsockname()[0] == '127.0.0.1'
        assert json.loads(descriptor.read_text())['port'] == sockets[0].getsockname()[1]
        signal.touch()
        if fail:
            raise RuntimeError('server failed')
        deadline = time.monotonic() + 3
        while not server.should_exit and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.should_exit

    server.run = run
    uvicorn = SimpleNamespace(Config=Mock(), Server=Mock(return_value=server))
    with patch.dict(os.environ, {'MINDOCK_SHUTDOWN_FILE': str(signal), 'MINDOCK_SESSION_FILE': str(descriptor)}), \
            patch.dict(sys.modules, {'uvicorn': uvicorn, 'app.main': SimpleNamespace(app=FastAPI())}), \
            patch.object(backend, 'parent_is_alive', return_value=True):
        if fail:
            with pytest.raises(RuntimeError, match='server failed'):
                backend.serve(root)
        else:
            backend.serve(root)
    assert not descriptor.exists()
    assert not signal.exists()


def test_serve_stops_for_parent_loss_without_shutdown_file(tmp_path):
    root = desktop.initialize_data_dir(tmp_path / 'runtime')
    server = SimpleNamespace(should_exit=False, started=True)

    def run(*, sockets):
        deadline = time.monotonic() + 3
        while not server.should_exit and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.should_exit

    server.run = run
    uvicorn = SimpleNamespace(Config=Mock(), Server=Mock(return_value=server))
    with patch.dict(os.environ, {'MINDOCK_SHUTDOWN_FILE': '', 'MINDOCK_SESSION_FILE': str(tmp_path / 'session.json')}), \
            patch.dict(sys.modules, {'uvicorn': uvicorn, 'app.main': SimpleNamespace(app=FastAPI())}), \
            patch.object(backend, 'parent_is_alive', return_value=False):
        backend.serve(root)


@pytest.mark.parametrize('bundled,frozen', [('1', False), ('', True)])
def test_bundled_and_frozen_runtime_use_certifi_for_maintenance(tmp_path, bundled, frozen):
    from app import runtime
    from scripts import backup_runtime
    threads = []

    def thread(*, target, **kwargs):
        instance = Mock()
        instance.target = target
        threads.append(instance)
        return instance

    def maintenance(*args, **kwargs):
        assert os.environ['SSL_CERT_FILE'] == str(tmp_path / 'cacert.pem')
        assert kwargs['allow_desktop_lock']
        assert threads[0].start.called
        return {'ok': True}

    with patch.dict(os.environ, {'MINDOCK_BUNDLED_RUNTIME': bundled, 'MINDOCK_PARENT_PID': '8765',
                                'MINDOCK_MAINTENANCE_OWNER_PID': '8765'}), \
            patch.object(sys, 'argv', ['backend', '--maintenance', 'inspect', '--archive', str(tmp_path / 'backup.zip')]), \
            patch.object(sys, 'frozen', frozen, create=True), \
            patch.object(runtime, 'data_dir', return_value=tmp_path), \
            patch('certifi.where', return_value=str(tmp_path / 'cacert.pem')), \
            patch.object(backend, 'Thread', side_effect=thread), \
            patch.object(backend, 'parent_is_alive', return_value=True), \
            patch.object(backup_runtime, 'run_maintenance', side_effect=maintenance):
        backend.main()
    threads[0].join.assert_called_once()


def test_maintenance_parent_loss_exits_recoverably():
    stopped = Mock()
    stopped.wait.return_value = False
    with patch.object(backend, 'parent_is_alive', return_value=False), \
            patch.object(backend.os, '_exit', side_effect=SystemExit(75)) as exit_process, \
            pytest.raises(SystemExit) as exit_info:
        backend._watch_parent(8765, stopped, lambda: os._exit(75))
    assert exit_info.value.code == 75
    exit_process.assert_called_once_with(75)


def test_shutdown_during_real_uvicorn_startup_runs_lifespan_cleanup(tmp_path):
    root = tmp_path / 'runtime'
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    signal, descriptor = private / 'shutdown', private / 'session.json'
    entered, cleaned = private / 'entered', private / 'cleaned'
    signal.touch()
    env = dict(os.environ, CYBERPAL_DATA_DIR=str(root), MINDOCK_SESSION_TOKEN='isolated-test',
               MINDOCK_SESSION_FILE=str(descriptor), MINDOCK_SHUTDOWN_FILE=str(signal),
               MINDOCK_PARENT_PID=str(os.getpid()), MINDOCK_BUNDLED_RUNTIME='1')
    env.pop('MINDOCK_RUNTIME_LOCKED', None)
    env.pop('MINDOCK_MAINTENANCE_OWNER_PID', None)
    code = (
        'import sys,time\nfrom pathlib import Path\nfrom app import main\n'
        'from desktop import backend\n'
        'entered,cleaned = sys.argv[1:]\nsys.argv[:] = sys.argv[:1]\n'
        'def startup():\n'
        '    Path(entered).touch()\n'
        '    time.sleep(.5)\n'
        'def shutdown():\n'
        '    Path(cleaned).touch()\n'
        'main.app.router.on_startup[:] = [startup]\n'
        'main.app.router.on_shutdown[:] = [shutdown]\n'
        'backend.main()\n'
    )
    result = subprocess.run([sys.executable, '-c', code, str(entered), str(cleaned)],
        cwd=Path(backend.__file__).resolve().parents[1], env=env, capture_output=True,
        text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert entered.exists()
    assert cleaned.exists(), 'A startup-time stop must run lifespan shutdown'
    assert not descriptor.exists()
    assert not signal.exists()
