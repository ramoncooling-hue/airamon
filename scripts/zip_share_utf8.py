#!/usr/bin/env python3
"""Create a portable UTF-8 source archive from a staged share directory."""

from __future__ import annotations

import sys
import os
import shutil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

DESKTOP_FILES = {
    'backend.py', 'backend.spec', 'MinDock.swift', 'OCR.swift', 'INSTALL.txt',
    'requirements-build.txt', 'requirements-lock.txt',
    'windows/after-pack.cjs', 'windows/main.cjs', 'windows/policy.cjs',
    'windows/runtime.cjs', 'windows/ocr.py', 'windows/package.json',
    'windows/package-lock.json', 'windows/requirements.txt',
    'windows/requirements-lock.txt', 'windows/runtime-artifacts.json',
    'windows/tests/policy.test.cjs', 'windows/tests/runtime.test.cjs',
}


def excluded(relative: Path) -> bool:
    parts = relative.parts
    return (
        "__pycache__" in parts
        or "node_modules" in parts
        or ".pytest_cache" in parts
        or ".mypy_cache" in parts
        or ".ruff_cache" in parts
        or ".tox" in parts
        or ".git" in parts
        or relative.suffix in {".pyc", ".pyo"}
        or relative.as_posix() == "docs/MinDock-品牌手册.png"
        or parts[:2] == ("docs", "superpowers")
        or (len(parts) == 2 and parts[0] == "docs" and parts[1].startswith("PROJECT-LAYOUT-DRY-RUN-"))
        or bool(set(parts) & {'output', 'data', '.cache', '.DS_Store', 'vector-index'})
        or any(part.startswith('.venv') for part in parts)
        or relative.name.lower() in {'settings.json', 'mcp.json', 'banxue.db', 'banxue.db-wal', 'banxue.db-shm', '.env'}
        or relative.name.lower().startswith('.env.')
    )


def stage_source_folders(root: Path, stage: Path) -> None:
    for folder in ('app', 'web', 'skills', 'agents', 'scripts', 'tests', 'docs', 'domains', 'desktop'):
        (stage / folder).mkdir(parents=True, exist_ok=True)
        for current, directories, files in os.walk(root / folder, followlinks=False):
            current = Path(current)
            directories[:] = [name for name in directories
                               if not (current / name).is_symlink()
                               and not excluded((current / name).relative_to(root))]
            for name in files:
                source = current / name
                relative = source.relative_to(root)
                if source.is_symlink() or excluded(relative):
                    continue
                if folder == 'desktop' and relative.relative_to('desktop').as_posix() not in DESKTOP_FILES:
                    continue
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)


def main() -> None:
    if sys.argv[1] == '--stage':
        stage_source_folders(Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve())
        return
    root = Path(sys.argv[1]).resolve()
    archive = Path(sys.argv[2]).resolve()
    with ZipFile(archive, "x", compression=ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(root)
                desktop_allowed = relative.parts[0] != 'desktop' or relative.relative_to('desktop').as_posix() in DESKTOP_FILES
                if desktop_allowed and not excluded(relative):
                    output.write(path, relative.as_posix())


if __name__ == "__main__":
    main()
