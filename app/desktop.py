"""Desktop-only data initialization and loopback session isolation."""

from __future__ import annotations

import hmac
import os
from pathlib import Path
import sys

from starlette.responses import JSONResponse


def get_parent_pid() -> int:
    value = os.environ.get('MINDOCK_PARENT_PID')
    pid = int(value) if value else os.getppid()
    if pid <= 0:
        raise ValueError('MINDOCK_PARENT_PID must be a positive process ID')
    return pid


def parent_is_alive(pid: int) -> bool:
    if sys.platform != 'win32':
        return os.getppid() == pid
    # Windows retains the original parent PID after exit. A process handle can
    # be waited on, including when its exit code happens to be STILL_ACTIVE.
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists
            return False
        if error == 5:  # Access denied is not evidence of process exit.
            return True
        raise OSError(error, 'Cannot open desktop parent process')
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        if result == 0xFFFFFFFF:
            raise OSError(ctypes.get_last_error(), 'Cannot wait on desktop parent process')
        return result == 258  # WAIT_TIMEOUT means the process is still running.
    finally:
        kernel.CloseHandle(handle)


def initialize_data_dir(root: Path) -> Path:
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    for folder in ('app', 'app/logs', 'app/backups', 'StudyPal', 'LifePal', 'WorkPal'):
        (root / folder).mkdir(parents=True, exist_ok=True, mode=0o700)
    if not os.access(root, os.W_OK):
        raise OSError(f'Data directory is not writable: {root}')
    return root


class DesktopAccessGuard:
    """Keep the embedded backend private without adding user accounts."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        from starlette.requests import Request
        request = Request(scope)
        supplied = request.headers.get('x-mindock-session') or request.cookies.get('mindock_session', '')
        host = request.url.hostname
        origin = request.headers.get('origin')
        expected = f'{request.url.scheme}://{request.headers.get("host", "")}'
        if (host != '127.0.0.1' or not hmac.compare_digest(supplied, self.token)
                or (origin and origin != expected)):
            return await JSONResponse({'detail': 'Desktop session required'}, status_code=403)(scope, receive, send)
        await self.app(scope, receive, send)
