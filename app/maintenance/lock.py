"""Cooperative, nonblocking lifetime lock with thread-local reentrancy."""

from contextlib import contextmanager
try:
    import fcntl
except ImportError:  # Windows source runner
    fcntl = None
    import msvcrt
import os
from pathlib import Path
import stat
import sys
import threading


class RuntimeBusyError(RuntimeError):
    pass


_owners = {}
_mutex = threading.RLock()


def owned_by_process(root):
    with _mutex:
        entry = _owners.get(checked_root(root))
        return bool(entry and entry[0][0] == os.getpid())


def _acquire(fd):
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'\0')
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError('Runtime lock unavailable') from exc


def _release(fd):
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextmanager
def check_legacy_desktop(root, *, allow_desktop_lock=False):
    """Protect CLI users from a pre-runtime-lock desktop backend (0.5.2)."""
    path = checked_root(checked_root(root) / 'app') / 'desktop.lock'
    if allow_desktop_lock:
        from ..desktop import get_parent_pid, parent_is_alive
        parent = get_parent_pid()
        if (os.environ.get('MINDOCK_MAINTENANCE_OWNER_PID') != str(parent)
                or not parent_is_alive(parent)):
            raise RuntimeBusyError('Native maintenance requires its parent shell PID')
        yield
        return
    if not path.exists():
        yield
        return
    if path.is_symlink():
        raise ValueError('desktop.lock must not be a symlink')
    fd = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    try:
        try:
            _acquire(fd)
        except BlockingIOError as exc:
            raise RuntimeBusyError('Desktop is running; quit it before standalone maintenance') from exc
        yield
    finally:
        os.close(fd)


def checked_root(root):
    path = Path(root).expanduser().absolute()
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            if (sys.platform == 'darwin' and str(ancestor) in {'/var', '/tmp', '/etc'}
                    and ancestor.resolve() == Path('/private') / ancestor.name):
                continue
            raise ValueError(f'Symlink runtime path is not allowed: {ancestor}')
    return path.resolve()


@contextmanager
def runtime_lock(root):
    """Acquire app/runtime.lock, or raise RuntimeBusyError without waiting.

    The lock file must never be deleted or replaced. Same-thread nesting is
    supported so startup can call apply_schema while holding the lifetime lock.
    Recover interrupted restores before allowing any database connection.
    """
    root = checked_root(root)
    app = checked_root(root / 'app')
    identity = (os.getpid(), threading.get_ident())
    with _mutex:
        held = _owners.get(root)
        if held:
            if held[0] != identity:
                raise RuntimeBusyError('Runtime is in use; stop the backend before maintenance')
            held[2] += 1
        else:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            app.mkdir(mode=0o700, parents=True, exist_ok=True)
            if (app / 'runtime.lock').is_symlink():
                raise ValueError('runtime.lock must not be a symlink')
            fd = os.open(app / 'runtime.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError('runtime.lock must be a regular file')
                _acquire(fd)
            except BaseException as exc:
                os.close(fd)
                if isinstance(exc, BlockingIOError):
                    raise RuntimeBusyError('Runtime is in use; stop the backend before maintenance') from exc
                raise
            _owners[root] = [identity, fd, 1]
    try:
        if not held:
            from .backup import recover_restore
            recover_restore(root)
        yield root
    finally:
        with _mutex:
            entry = _owners[root]
            entry[2] -= 1
            if entry[2] == 0:
                _release(entry[1])
                os.close(entry[1])
                del _owners[root]
