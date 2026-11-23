"""Bundled backend entry point; the native shell owns its lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path
from threading import Event, Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.desktop import get_parent_pid, parent_is_alive


def _watch_parent(parent, stopped, on_stop, shutdown_file=None):
    while not stopped.wait(0.2):
        if shutdown_file is not None and shutdown_file.exists():
            on_stop()
            return
        try:
            alive = parent_is_alive(parent)
        except OSError:
            # A transient process-query failure must not abandon the watchdog.
            continue
        if not alive:
            on_stop()
            return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh-seed', action='store_true')
    parser.add_argument('--migrate-from', type=Path)
    parser.add_argument('--maintenance', choices=['backup', 'restore', 'inspect'])
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--archive-sha256')
    args = parser.parse_args()
    os.umask(0o077)
    if getattr(sys, 'frozen', False) or os.environ.get('MINDOCK_BUNDLED_RUNTIME') == '1':
        import certifi
        os.environ['SSL_CERT_FILE'] = certifi.where()
    from app.desktop import initialize_data_dir
    from app.runtime import data_dir
    root = data_dir()
    if args.maintenance:
        if not args.archive:
            parser.error('--archive is required for maintenance')
        from scripts.backup_runtime import run_maintenance
        # The desktop shell remains open while its server is stopped.
        owner = os.environ.get('MINDOCK_MAINTENANCE_OWNER_PID', '')
        parent = get_parent_pid()
        if owner and (owner != str(parent) or not parent_is_alive(parent)):
            raise SystemExit('Native maintenance requires its live parent shell PID')
        allow_shell = bool(owner)
        stopped = Event()
        watcher = None
        if allow_shell:
            # Recovery journals make a parent-loss exit recoverable on next launch.
            watcher = Thread(target=lambda: _watch_parent(parent, stopped, lambda: os._exit(75)),
                             name='mindock-maintenance-parent', daemon=True)
            watcher.start()
        try:
            result = run_maintenance(args.maintenance, root, args.archive,
                                     allow_desktop_lock=allow_shell, expected_sha256=args.archive_sha256)
        except Exception as exc:
            print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
            raise SystemExit(1)
        finally:
            stopped.set()
            if watcher is not None:
                watcher.join()
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.migrate_from:
        from scripts.migrate_runtime import migrate
        print(json.dumps(migrate(args.migrate_from, root), ensure_ascii=False))
        return
    initialize_data_dir(root)
    if args.refresh_seed:
        from scripts.load_memory import main as load_seed
        from app.runtime import ROOT
        from app.maintenance.lock import runtime_lock, check_legacy_desktop
        with check_legacy_desktop(root), runtime_lock(root):
            load_seed(['--data-dir', str(ROOT / 'seed')])
        return

    if not os.environ.get('MINDOCK_SESSION_TOKEN'):
        raise SystemExit('Desktop session token is required; start MinDock.app.')
    from app.maintenance.lock import runtime_lock
    with runtime_lock(root):
        os.environ['MINDOCK_RUNTIME_LOCKED'] = '1'
        serve(root)


def serve(root: Path) -> None:
    import uvicorn
    from app.main import app
    parent = get_parent_pid()
    descriptor = Path(os.environ['MINDOCK_SESSION_FILE'])
    shutdown_value = os.environ.get('MINDOCK_SHUTDOWN_FILE')
    shutdown_file = Path(shutdown_value) if shutdown_value else None
    stopped = Event()
    watcher = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # The shell supplies a fresh private run path. A signal already present
        # here may have arrived during startup and must still request shutdown.
        if sys.platform == 'win32':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        saved_port = root / 'app' / 'desktop-port.json'
        preferred = 0
        try:
            preferred = int(json.loads(saved_port.read_text())['port'])
            if not 1024 <= preferred <= 65535 or preferred == 8899:
                preferred = 0
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            sock.bind(('127.0.0.1', preferred))
        except OSError:
            if not preferred:
                raise
            sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        saved_port.write_text(json.dumps({'port': port}))
        descriptor.write_text(json.dumps({'port': port}), encoding='utf-8')
        config = uvicorn.Config(app, host='127.0.0.1', port=port, access_log=False,
                                log_level='warning', timeout_graceful_shutdown=8)
        server = uvicorn.Server(config)

        def request_stop():
            # Uvicorn skips lifespan shutdown if should_exit is set during
            # startup. Keep the request until startup has fully completed.
            while not server.started:
                if stopped.wait(0.05):
                    return
            server.should_exit = True

        watcher = Thread(target=lambda: _watch_parent(parent, stopped, request_stop, shutdown_file),
                         name='mindock-desktop-parent', daemon=True)
        watcher.start()
        server.run(sockets=[sock])
    finally:
        stopped.set()
        if watcher is not None:
            watcher.join()
        sock.close()
        try:
            descriptor.unlink(missing_ok=True)
        finally:
            if shutdown_file is not None:
                shutdown_file.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
