"""Cooperative extraction cancellation and ownership of extractor subprocesses."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import signal
import subprocess
import tempfile
import time


class IngestCancelled(Exception):
    pass


_control = ContextVar('ingest_control', default=None)


@contextmanager
def controlled(cancelled, progress):
    token = _control.set((cancelled, progress))
    try:
        yield
    finally:
        _control.reset(token)


def checkpoint(stage=None, done=0, total=0):
    control = _control.get()
    if control:
        if control[0]():
            raise IngestCancelled()
        if stage:
            control[1](stage, done, total)


def run(command, *, timeout, **kwargs):
    """Drain to disk, poll cancellation, and always reap our own child group."""
    if not _control.get():
        return subprocess.run(command, timeout=timeout, **kwargs)
    checkpoint()
    # An independent reader keeps seek/read from moving the child's write offset.
    with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryFile() as stdout, \
            open(os.path.join(directory, 'stderr'), 'w+b', buffering=0) as stderr, \
            open(os.path.join(directory, 'stderr'), 'rb', buffering=0) as progress:
        proc = subprocess.Popen(command, stdout=stdout, stderr=stderr,
                                start_new_session=os.name == 'posix')
        started = time.monotonic()
        offset = 0
        pending = b''
        try:
            while True:
                checkpoint()
                if time.monotonic() - started > timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                exited = proc.poll() is not None
                # Native OCR emits small JSON progress lines on stderr.
                pread = getattr(os, 'pread', None)
                if pread:
                    chunk = pread(progress.fileno(), 65536, offset)
                else:
                    progress.seek(offset)
                    chunk = progress.read(65536)
                pending += chunk
                offset += len(chunk)
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    try:
                        value = json.loads(line)
                        if value.get('event') == 'progress':
                            checkpoint('ocr', int(value['page']), int(value['total']))
                    except (ValueError, KeyError, TypeError, AttributeError):
                        pass
                if exited and len(chunk) < 65536:
                    break
                if len(chunk) < 65536:
                    time.sleep(0.05)
            checkpoint()
            stdout.seek(0)
            stderr.seek(0)
            result = subprocess.CompletedProcess(command, proc.returncode,
                stdout.read().decode('utf-8', errors='replace'),
                stderr.read().decode('utf-8', errors='replace'))
            if kwargs.get('check'):
                result.check_returncode()
            return result
        finally:
            if proc.poll() is None:
                if os.name == 'posix':
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    if os.name == 'posix':
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
            proc.wait()
