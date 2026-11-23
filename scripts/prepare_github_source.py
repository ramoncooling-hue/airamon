#!/usr/bin/env python3
"""Export a deterministic, privacy-checked public source tree, never git history."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.zip_share_utf8 import DESKTOP_FILES as SHARE_DESKTOP_FILES, excluded

DESKTOP_FILES = SHARE_DESKTOP_FILES | {'windows/asr_probe.py', 'windows/tests/permissions.test.cjs'}

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = '\u542f\u52a8\u4f34\u5b66.command'
MANUAL_NAME = '\u5e73\u53f0\u5b9a\u4e49\u4e0e\u4f7f\u7528\u624b\u518c.md'
REPORT_FILE = 'GITHUB-SOURCE-MANIFEST.json'
EXPORT_MTIME = 946684800  # 2000-01-01 UTC, also valid for the existing ZIP writer.
ROOT_FILES = (
    '.gitignore', '.pre-commit-config.yaml', '.prettierrc.json', 'LICENSE',
    'THIRD-PARTY-NOTICES.md', 'CHANGELOG.md', 'VERSION', 'requirements.txt', 'requirements-dev.txt',
    'requirements-extensions.txt', 'requirements-retrieval-eval.txt',
    'pyproject.toml', 'pytest.ini', 'package.json', 'package-lock.json',
    'eslint.config.mjs', LAUNCHER, 'start_cyberpal.bat',
)
SCRIPT_FILES = tuple('scripts/' + name for name in (
    '__init__.py', 'backup_runtime.py', 'build_macos.py', 'build_share_zip.sh',
    'build_windows.py', 'check_extension_packaging.py', 'check_graph_health.py',
    'check_licenses.py', 'check_licenses.sh', 'coverage.sh', 'evaluate_answer_support.py',
    'evaluate_retrieval.py', 'graph_baseline.py', 'import_wiki_workpal.py', 'load_memory.py',
    'local_embedding_eval.py', 'local_extensions.py', 'migrate_runtime.py', 'organize_release_outputs.py',
    'evaluate_extraction.py', 'prepare_extraction_annotation.py', 'run_extraction_model.py',
    'prepare_github_source.py', 'project_layout_dry_run.py', 'rebuild_graph_data.py',
    'seed_workpal_graph.py', 'verify_all.sh', 'verify_local_install.py',
    'verify_macos.py', 'verify_mcp_desktop.py', 'verify_mcp_model.py',
    'verify_windows.py', 'zip_share_utf8.py',
))
DOC_FILES = (
    'docs/GITHUB-UPLOAD.md', 'docs/examples/bge-small-zh.model.json',
    'docs/examples/mcp.example.json', 'docs/examples/retrieval-eval.example.json',
    'docs/platform/Mac\u684c\u9762\u5b89\u88c5\u4e0e\u8d44\u6599\u5907\u4efd.md',
    'docs/platform/Windows\u684c\u9762\u5b89\u88c5\u4e0e\u8d44\u6599\u5907\u4efd.md',
    'docs/platform/\u672c\u5730\u6269\u5c55\u4e0e\u5411\u91cf\u68c0\u7d22.md',
)
DEMO_NAME = 'DEMO-\u9875\u9762\u7d22\u5f15.md'
GENERATED_FILES = ('README.md', 'docs/platform/' + MANUAL_NAME, DEMO_NAME)
SOURCE_SUFFIXES = {
    'app': {'.py', '.sql'},
    'web': {'.html', '.css', '.js', '.txt', '.md', '.png', '.ico', '.svg'},
    'agents': {'.json', '.md'}, 'skills': {'.json', '.md'},
    'domains': {'.json', '.md'}, 'tests': {'.py', '.cjs', '.json'},
    'seed/public': {'.json'},
}
FIXTURE_FILES = frozenset((
    'answer_support_zh.json', 'answer_support_holdout_zh.json',
    'answer_support_validation_zh.json', 'retrieval_zh.json',
    'retrieval_zh_supplement.json', 'retrieval_zh_adversarial.json',
    'mcp_server.py', 'mcp_model_server.py', 'mcp_http_server.py',
))
REQUIRED_FILES = tuple(sorted(set(ROOT_FILES + SCRIPT_FILES + DOC_FILES + GENERATED_FILES + (
    'app/main.py', 'app/schema/001_memory.sql', 'app/schema/030_agent_memory_action.sql',
    'web/index.html', 'web/assets/cytoscape.min.js', 'web/assets/cytoscape.LICENSE.txt',
    'web/assets/lucide.min.js', 'web/assets/LICENSE-lucide.txt',
    'web/assets/vendor/markdown-it.umd.min.js', 'web/assets/vendor/LICENSE-markdown-it.txt',
    'web/assets/vendor/purify.min.js', 'web/assets/vendor/LICENSE-dompurify.txt',
    'web/assets/vendor/LICENSE-dompurify-MPL.txt',
    'agents/laoba/manifest.json', 'agents/laoba/system.md', 'skills/laoba/SKILL.md',
    'domains/mba.json', 'seed/public/manifest.json', 'seed/public/dash_courses.json',
    'tests/test_github_source.py',
) + tuple('desktop/' + name for name in DESKTOP_FILES))))

# Only synthetic security-test literals in these exact reviewed bytes are allowed.
# A changed file must be re-reviewed; tests/ is not a blanket secret exemption.
REVIEWED_FIXTURES = {
    'tests/test_logging.py': '1eeccfef5718f7ffb23aed62db26b33a01810a0ea30284835a970014e3feb877',
    'tests/test_diagnostics.py': '76a003181f4985e2e727b097c59a1a9ec9df9c0b48da468516807c58cb424153',
    'tests/test_agent_runtime_security.py': '2d75b4ef747af744b4649fadfd146e1d998114fdb1a5bdc335a746898815599c',
    'tests/test_config_composer.py': '7abf8f0451dd8d4c3d889edaad59fb3c247012c10d9185e7a52ca0ca77e5bb9d',
    'tests/test_model_profiles.py': 'e2e3d170c7ab05d75c6f03877282022f5d7cef44d7b1c88135007196707f023b',
    'tests/test_personal_agent.py': '8043818afc6f7e78ebab23e0940e303a5e9fcd550fdc7b119051527c8f3d6857',
    'tests/test_vector_index.py': '77a57e9a6545592985cf1166f4aa0cbf2ec18d9e6ed9de9e952d7af69ab9e6bc',
    'desktop/windows/tests/policy.test.cjs': '0ceca2835ca554c213b58ae417d2089990f30175206a2123d14873495c706154',
}
SECRET_PATTERNS = (
    ('private-key', re.compile(rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----')),
    ('provider-key', re.compile(rb'\bsk-[A-Za-z0-9_-]{20,}')),
    ('github-key', re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})')),
    ('aws-key', re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b')),
    ('url-credentials', re.compile(rb'https?://[^\s/"\x27:@]+:[^\s/"\x27@]+@', re.I)),
    ('credential-literal', re.compile(
        rb'''["']?\b(?:api[_-]?key|password|passwd|client_secret|access_token|refresh_token|bearer_token)["']?\s*[:=]\s*["']([^"'\r\n]{16,})["']''', re.I)),
)
HOME_PATTERN = re.compile(rb'(?:/(?:Users|home)/([^/\s"\x27<>]+)|[A-Za-z]:[\\/]+Users[\\/]+([^\\/\s"\x27<>]+))')
SYNTHETIC_USERS = {b'me', b'someone', b'user', b'username', b'example', b'secret', b'x', b'Ada', b'Public', b'Shared'}
PLACEHOLDER_USER = '\u4f60\u7684\u7528\u6237\u540d'.encode('utf-8')
BLOCKED_PARTS = {
    'private', 'secrets', 'wiki', 'textbooks', 'materials', 'inbox', 'source',
    'screenshots', 'recordings', 'models', 'logs', 'archives', 'dist', 'build',
    'staging', '.github', '.ssh', '.playwright-cli', '.coverage', '.idea', '.vscode',
}
BLOCKED_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.pem', '.key', '.p12', '.pfx', '.log', '.zip', '.pdf', '.docx', '.pptx'}


class ExportError(ValueError):
    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report or {'privacy_scan': {'status': 'failed', 'errors': [], 'reviewed_fixtures': {}}}


def _blocked(relative):
    return (excluded(relative) or bool(set(part.lower() for part in relative.parts) & BLOCKED_PARTS)
            or any(part.startswith('.') for part in relative.parts)
            or relative.suffix.lower() in BLOCKED_SUFFIXES
            or relative.name.lower() in {'credentials.json', 'secrets.json', 'config.local.json'})


def _allowed(relative):
    name = relative.as_posix()
    if name in ROOT_FILES + SCRIPT_FILES + DOC_FILES:
        return True
    if _blocked(relative):
        return False
    if relative.parts[0] == 'desktop':
        return relative.relative_to('desktop').as_posix() in DESKTOP_FILES
    if relative.parts[0] == 'tests':
        return (len(relative.parts) == 2 and (
            relative.name.startswith('test_') and relative.suffix == '.py'
            or relative.suffix == '.cjs')) or (
            relative.parts[:2] == ('tests', 'fixtures') and len(relative.parts) == 3
            and relative.name in FIXTURE_FILES)
    if relative.parts[:2] == ('seed', 'public'):
        return len(relative.parts) == 3 and relative.suffix == '.json'
    folder = relative.parts[0]
    if folder in {'agents', 'skills'}:
        return len(relative.parts) == 3 and relative.name in {'manifest.json', 'system.md', 'SKILL.md'}
    if folder == 'domains':
        return len(relative.parts) == 2 and relative.suffix in SOURCE_SUFFIXES['domains']
    return folder in {'app', 'web'} and relative.suffix in SOURCE_SUFFIXES[folder]


def _source_paths(root):
    # Do not traverse the repository root: data/, output/ and .git/ stay unread.
    names = set(ROOT_FILES + SCRIPT_FILES + DOC_FILES)
    for folder in (*SOURCE_SUFFIXES, 'desktop'):
        base = root / folder
        if not base.is_dir():
            continue
        if any((root / Path(*base.relative_to(root).parts[:i])).is_symlink()
               for i in range(1, len(base.relative_to(root).parts) + 1)):
            continue
        for current, directories, files in os.walk(base, followlinks=False):
            current = Path(current)
            directories[:] = sorted(name for name in directories
                                    if not (current / name).is_symlink()
                                    and not _blocked((current / name).relative_to(root)))
            names.update((current / name).relative_to(root).as_posix() for name in files)
    for name in sorted(names):
        relative = Path(name)
        path = root / relative
        if not _allowed(relative) or not path.is_file():
            continue
        if any((root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
            continue
        yield relative


def _public_readme(root):
    text = (root / 'docs/GITHUB-UPLOAD.md').read_text(encoding='utf-8')
    start, end = '<!-- PUBLIC-README:START -->', '<!-- PUBLIC-README:END -->'
    if text.count(start) != 1 or text.count(end) != 1 or text.index(end) < text.index(start):
        raise ExportError('Public README markers missing or ambiguous')
    return text.split(start, 1)[1].split(end, 1)[0].strip() + '\n'


def _scan(files, reviewed):
    errors, exemptions = [], {}
    for name, data, _mode in files:
        sha = hashlib.sha256(data).hexdigest()
        reviewed_fixture = (name.startswith('tests/test_') or name.startswith('desktop/windows/tests/')) and reviewed.get(name) == sha
        if reviewed_fixture:
            exemptions[name] = sha
        for kind, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(data):
                if kind == 'credential-literal':
                    value = match.group(1).lower()
                    if any(marker in value for marker in (b'example', b'placeholder', b'your_', b'${', b'redacted')):
                        continue
                if reviewed_fixture:
                    continue
                errors.append({'path': name, 'line': data.count(b'\n', 0, match.start()) + 1, 'rule': kind})
        for match in HOME_PATTERN.finditer(data):
            user = match.group(1) or match.group(2)
            if user == PLACEHOLDER_USER or ((name.startswith('tests/') or name.startswith('desktop/windows/tests/'))
                                           and user in SYNTHETIC_USERS):
                continue
            errors.append({'path': name, 'line': data.count(b'\n', 0, match.start()) + 1, 'rule': 'personal-home-path'})
    return {'status': 'failed' if errors else 'passed', 'errors': errors,
            'reviewed_fixtures': exemptions, 'files_scanned': len(files),
            'scope': 'allowlisted bytes only; personal data and git history are not read',
            'excluded_roots': ['data', '.git', 'output', '_archive', '.venv*', 'node_modules',
                               'docs/brand*', 'docs/design-previews', 'docs/superpowers', 'docs/NEW-WORKSPACE']}


def _version(files):
    by_name = {name: data for name, data, _mode in files}
    version = by_name.get('VERSION', b'').decode('ascii').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ExportError('Invalid source version')
    try:
        package = json.loads(by_name['desktop/windows/package.json'])
        lock = json.loads(by_name['desktop/windows/package-lock.json'])
        if any(value != version for value in (package['version'], lock['version'], lock['packages']['']['version'])):
            raise ExportError('Desktop/source version mismatch')
    except (KeyError, ValueError) as exc:
        if isinstance(exc, ExportError):
            raise
        raise ExportError('Invalid desktop version metadata') from None
    return version


def prepare_source(root, output, *, reviewed_fixtures=None):
    root = Path(root).expanduser().resolve()
    raw_output = Path(output).expanduser().absolute()
    if any(p.is_symlink() for p in (raw_output, *raw_output.parents)):
        raise ExportError('Refusing symlink output path')
    output = raw_output.resolve()
    if output.exists():
        raise ExportError('Refusing to overwrite existing output')
    if root.is_relative_to(output) or (output.is_relative_to(root)
                                      and not output.is_relative_to(root / 'output/github')):
        raise ExportError('Output overlaps protected source paths')
    files = []
    for relative in _source_paths(root):
        source = root / relative
        data = source.read_bytes()
        mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
        files.append((relative.as_posix(), data, mode))
        if relative.parts[:2] == ('seed', 'public'):
            files.append(('seed/' + relative.name, data, mode))
    missing = sorted(set(REQUIRED_FILES) - set(GENERATED_FILES) - {name for name, _data, _mode in files})
    if missing:
        raise ExportError('Missing required public inputs: ' + ', '.join(missing))
    public_readme = _public_readme(root).encode('utf-8')
    files.extend((name, public_readme, 0o644) for name in GENERATED_FILES if name != DEMO_NAME)
    files.append((DEMO_NAME, b'# MinDock pages\n\nSee README.md for installation, usage and feature boundaries.\n', 0o644))
    files.sort(key=lambda item: item[0])
    version = _version(files)
    privacy = _scan(files, REVIEWED_FIXTURES if reviewed_fixtures is None else reviewed_fixtures)
    report = {'format': 'mindock.github-source.v1', 'version': version,
              'file_count': len(files), 'privacy_scan': privacy,
              'files': [{'path': name, 'size': len(data), 'mode': format(mode, '04o'),
                         'sha256': hashlib.sha256(data).hexdigest()} for name, data, mode in files],
              'generated_files': list(GENERATED_FILES),
              'report_file': REPORT_FILE,
              'reproducibility': 'sorted paths, captured bytes, normalized modes and mtimes; report excludes itself',
              'validation': 'Export and privacy scan only; run build/tests separately before release.'}
    if privacy['errors']:
        raise ExportError('Privacy scan failed; no public tree was created', report)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.github-source-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'source'
        stage.mkdir()
        for name, data, mode in files:
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(mode)
            os.utime(target, (EXPORT_MTIME, EXPORT_MTIME))
        manifest = stage / REPORT_FILE
        manifest.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        manifest.chmod(0o644)
        os.utime(manifest, (EXPORT_MTIME, EXPORT_MTIME))
        for directory in sorted((p for p in stage.rglob('*') if p.is_dir()), reverse=True):
            directory.chmod(0o755)
            os.utime(directory, (EXPORT_MTIME, EXPORT_MTIME))
        # Reserve the destination exclusively; never replace an existing output.
        output.mkdir()
        try:
            for child in stage.iterdir():
                child.rename(output / child.name)
            output.chmod(0o755)
            os.utime(output, (EXPORT_MTIME, EXPORT_MTIME))
        except BaseException:
            shutil.rmtree(output)
            raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, help='New directory; defaults to output/github/MinDock-<VERSION>')
    parser.add_argument('--report', type=Path, help='Optional new JSON report, including sanitized failures')
    args = parser.parse_args()
    if args.report and (args.report.exists() or args.report.is_symlink()):
        print('Refusing to overwrite report', file=sys.stderr)
        return 2
    report = None
    report_allowed = False
    try:
        output = args.output or args.root / 'output/github' / ('MinDock-' + (args.root / 'VERSION').read_text().strip())
        if args.report:
            report_path = args.report.absolute()
            source_root = args.root.resolve()
            if (report_path.resolve().is_relative_to(source_root)
                    and not report_path.resolve().is_relative_to(source_root / 'output/github')):
                raise ExportError('Report must be outside source or under output/github')
            if report_path.resolve().is_relative_to(output.resolve()):
                raise ExportError('External report must be outside the exported directory')
            if any(p.is_symlink() for p in (report_path, *report_path.parents)):
                raise ExportError('Refusing symlink report path')
            report_allowed = True
        report = prepare_source(args.root, output)
        code = 0
    except ExportError as exc:
        report = exc.report
        print(str(exc), file=sys.stderr)
        code = 2
    except OSError:
        print('Export I/O failed; check inputs and choose a new output directory', file=sys.stderr)
        code = 2
    if report_allowed and report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n')
    if report:
        print(json.dumps({'version': report.get('version'), 'file_count': report.get('file_count', 0),
                          'privacy_scan': report['privacy_scan']}, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
