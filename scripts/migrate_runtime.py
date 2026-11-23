#!/usr/bin/env python3
"""Move a runtime snapshot to an empty destination; never merge databases."""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import sys

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime import DEFAULT_DATA_DIR
from app.maintenance import runtime_lock
from app.maintenance.lock import checked_root, check_legacy_desktop
from app.maintenance.files import (DB_REL, fsync_dir, relocate, rename_new,
                                   snapshot_tree, sync_tree, remap as _remap)


def migrate(source: Path, target: Path, *, merge: bool = False) -> dict[str, str]:
    if merge:
        raise ValueError('Database merge is unsupported; migrate only to an empty directory')
    source, target = checked_root(source), checked_root(target)
    if target == source or target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError('Source and target data directories must not contain each other')
    if not source.is_dir():
        raise ValueError(f'Source data directory does not exist: {source}')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(f'Target is not empty; refusing to overwrite: {target}')
    target.parent.mkdir(parents=True, exist_ok=True)
    with check_legacy_desktop(source), runtime_lock(source):
        from app.maintenance.migrations import recover_upgrade
        recover_upgrade(source / DB_REL)
        with tempfile.TemporaryDirectory(prefix='.maintenance-migrate-', dir=target.parent) as temporary:
            stage = Path(temporary) / 'data'
            stage.mkdir(mode=0o700)
            snapshot_tree(source, stage)
            relocate(stage, source, target, verify=True)
            sync_tree(stage)
            if target.exists():
                if not target.is_dir() or any(target.iterdir()):
                    raise ValueError('Target became occupied during migration')
                target.rmdir()
            rename_new(stage, target)
            fsync_dir(target.parent)
    return {'source': str(source), 'target': str(target), 'database': str(target / DB_REL)}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Migrate MinDock runtime to an empty directory; stop the backend first')
    parser.add_argument('--from', dest='source', type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument('--to', dest='target', type=Path, required=True)
    args = parser.parse_args(argv)
    print(migrate(args.source, args.target))


if __name__ == '__main__':
    main()
