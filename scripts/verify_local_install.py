#!/usr/bin/env python3
"""Verify the publishable CyberPal tree and an isolated runtime data root."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def verify(data_dir: Path) -> dict:
    data_root = data_dir.expanduser().resolve()
    os.environ['CYBERPAL_DATA_DIR'] = str(data_root)
    from app.memory import db

    # The verifier is also used by in-process tests and packaging checks. Keep
    # repeated calls isolated even when app.memory.db is already imported.
    db.DATA_DIR = data_root
    db.APP_DIR = data_root / 'app'
    db.DB_PATH = db.APP_DIR / 'banxue.db'

    required = [ROOT / 'app', ROOT / 'web' / 'index.html', ROOT / 'seed', ROOT / 'skills', ROOT / 'agents']
    missing = [str(path.relative_to(ROOT)) for path in required if not path.exists()]
    manifests = list((ROOT / 'skills').glob('*/manifest.json')) + list((ROOT / 'agents').glob('*/manifest.json'))
    invalid_manifests = []
    for path in manifests:
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            if not payload.get('key'):
                invalid_manifests.append(str(path.relative_to(ROOT)))
        except (OSError, ValueError):
            invalid_manifests.append(str(path.relative_to(ROOT)))
    secret_hits = []
    secret_pattern = re.compile(r'sk-[A-Za-z0-9]{12,}|api_key\s*[:=]\s*["\'][^"\']+', re.I)
    for path in list((ROOT / 'seed').rglob('*')) + list((ROOT / 'skills').rglob('*')) + list((ROOT / 'agents').rglob('*')):
        if path.is_file():
            text = path.read_text(encoding='utf-8', errors='ignore')
            if secret_pattern.search(text):
                secret_hits.append(str(path.relative_to(ROOT)))
    db.apply_schema()
    counts = db.table_counts()
    from app.hub import registry
    hub = registry.list_registry()
    hub_invalid = [
        f"{item['kind']}:{item['key']}"
        for item in hub.get('skills', []) + hub.get('agents', [])
        if not item.get('key') or not item.get('name') or not item.get('version')
    ]
    return {'ok': not missing and not invalid_manifests and not secret_hits and not hub_invalid,
            'data_dir': str(data_root), 'missing': missing,
            'invalid_manifests': invalid_manifests, 'secret_hits': secret_hits,
            'hub_invalid': hub_invalid, 'hub': {
                'skills': len(hub.get('skills', [])),
                'agents': len(hub.get('agents', [])),
            }, 'tables': counts}


def main() -> None:
    parser = argparse.ArgumentParser(description='验证 CyberPal 本地安装目录')
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data' / '_verify')
    args = parser.parse_args()
    result = verify(args.data_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['ok'] else 1)


if __name__ == '__main__':
    main()
