"""Offline maintenance. Native mode preserves the shell's desktop.lock.

Hold runtime_lock(root) for the backend's entire lifetime, including startup.
backup_runtime/restore_runtime acquire it themselves; callers must stop the
backend first. apply_schema() is reentrant within the owning startup thread.
"""

from .lock import RuntimeBusyError, runtime_lock


def backup_runtime(root, archive, *, allow_desktop_lock=False):
    from .backup import backup_runtime as backup
    return backup(root, archive, allow_desktop_lock=allow_desktop_lock)


def restore_runtime(root, archive, *, allow_desktop_lock=False, expected_sha256=None):
    from .backup import restore_runtime as restore
    return restore(root, archive, allow_desktop_lock=allow_desktop_lock, expected_sha256=expected_sha256)


def inspect_archive(archive):
    from .backup import inspect_archive as inspect
    return inspect(archive)


__all__ = ['runtime_lock', 'RuntimeBusyError', 'backup_runtime', 'restore_runtime', 'inspect_archive']
