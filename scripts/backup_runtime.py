#!/usr/bin/env python3
"""Offline local recovery CLI; callable from the frozen desktop backend."""

import argparse
import json
from pathlib import Path
import sys

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.maintenance import backup_runtime, restore_runtime, inspect_archive
from app.runtime import data_dir


def run_maintenance(action, root, archive, *, allow_desktop_lock=False, expected_sha256=None):
    """Native caller sets allow_desktop_lock only after stopping its backend."""
    if action == 'backup':
        return backup_runtime(root, archive, allow_desktop_lock=allow_desktop_lock)
    if action == 'restore':
        return restore_runtime(root, archive, allow_desktop_lock=allow_desktop_lock,
                               expected_sha256=expected_sha256)
    if action == 'inspect':
        return inspect_archive(archive)
    raise ValueError('Unknown maintenance action')


def main(argv=None):
    parser = argparse.ArgumentParser(description='MinDock local recovery; stop the backend first')
    parser.add_argument('--data-dir', type=Path, default=data_dir())
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('backup').add_argument('--output', type=Path, required=True)
    restore = sub.add_parser('restore')
    restore.add_argument('--input', type=Path, required=True)
    restore.add_argument('--expected-sha256')
    sub.add_parser('inspect').add_argument('--input', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_maintenance(args.action, args.data_dir, args.output if args.action == 'backup' else args.input,
                                 expected_sha256=getattr(args, 'expected_sha256', None))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
