"""Public source exports must be complete, deterministic and fail closed."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts import prepare_github_source as exporter


ROOT = Path(__file__).resolve().parents[1]


def put(root, name, content='public fixture\n'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return path


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source'
    for name in exporter.ROOT_FILES + exporter.SCRIPT_FILES + exporter.DOC_FILES:
        put(root, name)
    for name in exporter.DESKTOP_FILES:
        put(root, 'desktop/' + name)
    for name in exporter.REQUIRED_FILES:
        if name not in exporter.GENERATED_FILES:
            put(root, name)
    put(root, 'VERSION', '0.6.4\n')
    for name in ('desktop/windows/package.json', 'desktop/windows/package-lock.json'):
        put(root, name, json.dumps({'version': '0.6.4', 'packages': {'': {'version': '0.6.4'}}}))
    shutil.copy2(ROOT / 'docs/GITHUB-UPLOAD.md', root / 'docs/GITHUB-UPLOAD.md')
    put(root, 'app/main.py', 'PUBLIC = True\n')
    put(root, 'app/schema/030_agent_memory_action.sql', '-- published bytes\nSELECT 1;\n')
    put(root, 'agents/laoba/system.md')
    put(root, 'skills/wiki-extract/SKILL.md')
    put(root, 'domains/work.json', '{}')
    put(root, 'seed/public/manifest.json', '{}')
    put(root, 'seed/public/dash_courses.json', '[]')
    put(root, 'web/assets/mindock-mark.png', 'fake public image')
    (root / exporter.LAUNCHER).chmod(0o755)
    return root


def snapshot(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mode & 0o777)
            for p in sorted(root.rglob('*')) if p.is_file()}


def test_export_is_complete_and_reproducible_without_private_inputs(source, tmp_path):
    private = [
        'README.md', 'data/app/banxue.db', '.git/config', '.env', 'seed/private.json',
        'output/old.zip', '_archive/old.md', 'node_modules/pkg/index.js',
        'docs/brand/manual.png', 'docs/design-previews/board.html',
        'docs/superpowers/specs/design.md', 'docs/NEW-WORKSPACE/internal.md',
        'docs/platform/design.md', 'docs/brand-manual-assets/long.png',
        'docs/AGENT-HANDOFF-internal.md', 'app/settings.json', 'tests/.env.local',
        'tests/private/wiki.md', 'app/memory/private/notes.py', 'web/assets/textbook.pdf',
        'desktop/unknown.bin', 'scripts/unknown.py', 'app/__pycache__/main.pyc',
    ]
    for name in private:
        put(source, name, 'private content must stay local')
    original = snapshot(source)
    one, two = tmp_path / 'one', tmp_path / 'two'
    first = exporter.prepare_source(source, one)
    second = exporter.prepare_source(source, two)
    assert first == second
    assert snapshot(one) == snapshot(two)
    assert snapshot(source) == original
    names = set(snapshot(one))
    assert set(exporter.REQUIRED_FILES) <= names
    assert not (set(private) - {'README.md'}) & names
    assert 'private content' not in (one / 'README.md').read_text()
    assert first['privacy_scan']['errors'] == []
    assert first['privacy_scan']['status'] == 'passed'
    assert first['version'] == '0.6.4'
    assert first['file_count'] == len(first['files'])
    for row in first['files']:
        target = one / row['path']
        assert hashlib.sha256(target.read_bytes()).hexdigest() == row['sha256']
        assert target.stat().st_size == row['size']
        assert int(row['mode'], 8) == target.stat().st_mode & 0o777
    assert (one / exporter.LAUNCHER).stat().st_mode & 0o111
    assert (one / 'app/schema/030_agent_memory_action.sql').read_bytes() == (
        source / 'app/schema/030_agent_memory_action.sql').read_bytes()
    assert (one / 'seed/dash_courses.json').read_bytes() == (one / 'seed/public/dash_courses.json').read_bytes()


@pytest.mark.parametrize('make_secret', [
    lambda: 'sk-' + 'Az90' * 10,
    lambda: 'ghp_' + 'Ab72' * 10,
    lambda: '-----BEGIN ' + 'PRIVATE KEY-----\nabc\n',
    lambda: 'api_key = "' + 'production-value-' * 3 + '"',
    lambda: 'password: "' + 'actual-value-' * 3 + '"',
    lambda: '{"access_token": "' + 'live-value-' * 3 + '"}',
    lambda: 'https://' + 'user:real-password@example.org/data',
    lambda: '/Users/' + 'realperson/Documents/private-wiki',
    lambda: 'C:\\Users\\' + 'realperson\\Documents\\private',
], ids=['model-key', 'github-key', 'private-key', 'api-key', 'password', 'token',
        'url-credentials', 'mac-home', 'windows-home'])
def test_private_content_blocks_publication_without_echoing_values(source, tmp_path, make_secret):
    secret = make_secret()
    put(source, 'app/main.py', secret)
    target = tmp_path / 'export'
    with pytest.raises(exporter.ExportError) as error:
        exporter.prepare_source(source, target)
    assert not target.exists()
    report = json.dumps(error.value.report)
    assert secret not in report and secret not in str(error.value)
    assert error.value.report['privacy_scan']['errors'][0]['path'] == 'app/main.py'


def test_fake_test_credentials_need_exact_reviewed_content_digest(source, tmp_path):
    path = put(source, 'tests/test_logging.py', 'KEY = "sk-' + 'Az90' * 10 + '"\n')
    with pytest.raises(exporter.ExportError):
        exporter.prepare_source(source, tmp_path / 'blocked')
    reviewed = {'tests/test_logging.py': hashlib.sha256(path.read_bytes()).hexdigest()}
    report = exporter.prepare_source(source, tmp_path / 'allowed', reviewed_fixtures=reviewed)
    assert report['privacy_scan']['reviewed_fixtures'] == reviewed
    path.write_text(path.read_text() + '# changed\n')
    with pytest.raises(exporter.ExportError):
        exporter.prepare_source(source, tmp_path / 'changed', reviewed_fixtures=reviewed)


def test_symlinks_are_never_followed_and_required_links_fail(source, tmp_path):
    outside = put(tmp_path, 'outside.py', 'do not read')
    (source / 'app/linked.py').symlink_to(outside)
    (source / 'app/linked-dir').symlink_to(tmp_path, target_is_directory=True)
    target = tmp_path / 'export'
    exporter.prepare_source(source, target)
    assert not (target / 'app/linked.py').exists()
    assert not (target / 'app/linked-dir').exists()
    launcher = source / exporter.LAUNCHER
    launcher.unlink()
    launcher.symlink_to(outside)
    with pytest.raises(exporter.ExportError, match='required'):
        exporter.prepare_source(source, tmp_path / 'missing-launcher')


def test_refuses_existing_output_and_source_overlap(source, tmp_path):
    target = tmp_path / 'existing'
    put(target, 'sentinel', 'preserve')
    with pytest.raises(exporter.ExportError, match='overwrite'):
        exporter.prepare_source(source, target)
    assert (target / 'sentinel').read_text() == 'preserve'
    for destination in (source, source / 'app/export', source / 'data/export', source.parent):
        with pytest.raises(exporter.ExportError):
            exporter.prepare_source(source, destination)
    allowed = source / 'output/github/MinDock-0.6.4'
    exporter.prepare_source(source, allowed)
    assert (allowed / exporter.REPORT_FILE).is_file()


def test_missing_build_inputs_and_version_mismatch_block_export(source, tmp_path):
    required = source / 'requirements-dev.txt'
    required.unlink()
    with pytest.raises(exporter.ExportError, match='required'):
        exporter.prepare_source(source, tmp_path / 'missing')
    put(source, 'requirements-dev.txt')
    put(source, 'desktop/windows/package.json', '{"version":"0.6.3"}')
    with pytest.raises(exporter.ExportError, match='version'):
        exporter.prepare_source(source, tmp_path / 'mismatched')


def test_document_whitelist_and_public_readme_do_not_copy_internal_design(source, tmp_path):
    put(source, 'README.md', 'INTERNAL DESIGN AND PERSONAL PATH')
    put(source, 'docs/platform/' + exporter.MANUAL_NAME, 'INTERNAL DESIGN')
    out = tmp_path / 'export'
    exporter.prepare_source(source, out)
    text = (out / 'README.md').read_text()
    manual = (out / 'docs/platform' / exporter.MANUAL_NAME).read_text()
    assert 'INTERNAL DESIGN' not in text + manual
    assert 'requirements-dev.txt' in text and 'build_macos.py' in text and 'build_windows.py' in text
    assert 'MCP' in manual and '连接并发现工具' in manual and '课程材料上传与存储' in manual


def test_export_contains_legacy_share_builder_copy_inputs(source, tmp_path):
    for script in ('build_share_zip.sh', 'zip_share_utf8.py'):
        shutil.copy2(ROOT / 'scripts' / script, source / 'scripts' / script)
    out = tmp_path / 'export'
    exporter.prepare_source(source, out)
    assert (out / 'CHANGELOG.md').is_file()
    assert (out / 'DEMO-页面索引.md').is_file()
    assert 'README.md' in (out / 'DEMO-页面索引.md').read_text()
    packaged = subprocess.run(['bash', str(out / 'scripts/build_share_zip.sh'),
                              str(tmp_path / 'share'), str(tmp_path / 'share-tree')],
                             cwd=out, text=True, capture_output=True)
    assert packaged.returncode == 0, packaged.stderr
    assert (tmp_path / 'share-tree/README.md').is_file()


def test_cli_writes_only_sanitized_failure_report(source, tmp_path):
    secret = 'sk-' + 'Hz28' * 10
    put(source, 'app/main.py', secret)
    report = tmp_path / 'failure.json'
    out = tmp_path / 'export'
    result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/prepare_github_source.py'),
                             '--root', str(source), '--output', str(out), '--report', str(report)],
                            text=True, capture_output=True)
    assert result.returncode == 2
    assert secret not in result.stdout + result.stderr + report.read_text()
    assert json.loads(report.read_text())['privacy_scan']['status'] == 'failed'
    assert not out.exists()
    before = report.read_bytes()
    again = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/prepare_github_source.py'),
                            '--root', str(source), '--output', str(out), '--report', str(report)],
                           text=True, capture_output=True)
    assert again.returncode == 2 and report.read_bytes() == before


def test_policy_includes_all_current_build_tools_tests_and_vendored_licenses():
    assert set(p.relative_to(ROOT).as_posix() for p in (ROOT / 'scripts').iterdir()
               if p.suffix in {'.py', '.sh'}) <= set(exporter.SCRIPT_FILES)
    assert set(p.relative_to(ROOT).as_posix() for p in ROOT.glob('requirements*.txt')) <= set(exporter.ROOT_FILES)
    for relative in ('requirements-dev.txt', 'package-lock.json', 'pytest.ini',
                     'THIRD-PARTY-NOTICES.md', 'web/assets/vendor/LICENSE-markdown-it.txt',
                     'web/assets/vendor/LICENSE-dompurify.txt', 'web/assets/cytoscape.LICENSE.txt'):
        assert relative in exporter.REQUIRED_FILES
