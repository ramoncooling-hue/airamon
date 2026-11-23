#!/usr/bin/env python3
"""Reversibly archive explicitly selected historical output directories."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL = (
    'output/branding', 'output/graph-desktop-20261016', 'output/guide',
    'output/macos-mcp-20261014', 'output/macos-settings-20261015',
    'output/mcp-acceptance-20261014', 'output/mcp-acceptance-20261015',
    'output/pc-ui-20261016', 'output/playwright', 'output/retrieval-eval',
    'output/share', 'output/share-mcp-20261015-final', 'output/share-source-v0.5.0',
    'output/windows-mcp-20261014', 'test_localsystem.txt',
)


def organize(root=ROOT, *, apply=False, stamp=None):
    root = root.resolve()
    archive = root / '_archive' / (stamp or datetime.now().strftime('%Y%m%d-%H%M%S-release-cleanup'))
    plan = []
    for relative in HISTORICAL:
        source = root / relative
        if not source.exists():
            continue
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError('Refusing linked archive input: ' + relative)
        plan.append({'source': relative, 'destination': str((archive / relative).relative_to(root)),
                     'status': 'planned'})
    report = {'mode': 'apply' if apply else 'dry-run', 'moves': plan,
              'protected': ['data', '.git', '.venv*', 'output/models', 'output/macos',
                            'output/windows', 'output/verify', 'docs', 'app', 'web']}
    if not apply or not plan:
        return report
    archive.mkdir(parents=True, exist_ok=False)
    manifest = archive / 'manifest.json'
    manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    for item in plan:
        source, destination = root / item['source'], root / item['destination']
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        item['status'] = 'moved'
        manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Move only the reviewed historical allowlist.')
    args = parser.parse_args()
    print(json.dumps(organize(apply=args.apply), ensure_ascii=False, indent=2))
