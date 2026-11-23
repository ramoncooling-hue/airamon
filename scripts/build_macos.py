#!/usr/bin/env python3
"""Build a standalone Apple Silicon app and drag-install DMG, without user data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(*args: str, **kwargs) -> None:
    subprocess.run(args, check=True, **kwargs)


def source_snapshot() -> dict[str, str]:
    files = []
    for name in ('app', 'desktop', 'web', 'seed/public', 'skills', 'agents', 'domains', 'docs/platform', 'scripts'):
        files.extend(path for path in (ROOT / name).rglob('*')
                     if path.is_file() and not {'__pycache__', 'node_modules'}.intersection(path.parts) and path.suffix != '.pyc')
    files += [ROOT / name for name in ('VERSION', 'LICENSE', 'requirements.txt', 'requirements-extensions.txt')]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(files)}


def native_files(app: Path) -> list[Path]:
    magic = {b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}
    result = []
    for path in app.rglob('*'):
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as handle:
                if handle.read(4) in magic:
                    result.append(path)
    return sorted(result, key=lambda path: len(path.parts), reverse=True)


def verify_private_files(resources: Path, *, credential_files: tuple[Path, ...] = ()) -> None:
    # Never discover or read the developer's personal runtime configuration.
    # Known-value scans are opt-in, using explicitly supplied audit/fixture files.
    forbidden = [str(path.relative_to(resources)) for path in resources.rglob('*')
                 if path.name.lower() in {'settings.json', 'banxue.db', 'banxue.db-wal', 'banxue.db-shm', '.env', 'mcp.json'}
                 or path.name.lower().startswith('.env.')
                 or 'vector-index' in path.relative_to(resources).parts
                 or ('extensions' in path.relative_to(resources).parts and path.suffix.lower() == '.json')]
    secrets = set()

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = ''.join(char for char in key.lower() if char.isalnum())
                credential = any(word in normalized for word in
                                 ('apikey', 'token', 'password', 'secret', 'authorization', 'cookie', 'credential'))
                if credential and isinstance(item, str) and len(item) >= 8:
                    secrets.add(item.encode())
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for settings in credential_files:
        collect(json.loads(settings.read_text()))
    for path in resources.rglob('*'):
        if not path.is_file() or path.is_symlink() or not secrets:
            continue
        with path.open('rb') as handle:
            tail = b''
            overlap = max(map(len, secrets)) - 1
            while chunk := handle.read(1024 * 1024):
                content = tail + chunk
                if any(secret in content for secret in secrets):
                    forbidden.append(str(path.relative_to(resources)))
                    break
                tail = content[-overlap:] if overlap else b''
    if forbidden:
        raise RuntimeError('Private runtime content found in bundle: ' + ', '.join(sorted(set(forbidden))))


def sign_app(app: Path, identity: str) -> None:
    options = ['--options', 'runtime', '--timestamp'] if identity != '-' else []
    for path in native_files(app):
        run('/usr/bin/codesign', '--force', '--sign', identity, *options, str(path))
    for framework in sorted(app.rglob('*.framework'), key=lambda path: len(path.parts), reverse=True):
        if not framework.is_symlink():
            run('/usr/bin/codesign', '--force', '--sign', identity, *options, str(framework))
    run('/usr/bin/codesign', '--force', '--sign', identity, *options, str(app))
    run('/usr/bin/codesign', '--verify', '--deep', '--strict', str(app))


def build(output: Path, identity: str = '-', notary_profile: str | None = None) -> dict:
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise RuntimeError('Build on an Apple Silicon Mac with Xcode Command Line Tools.')
    version = (ROOT / 'VERSION').read_text().strip()
    if notary_profile and identity == '-':
        raise ValueError('Notarization requires a Developer ID Application identity.')
    from scripts.check_extension_packaging import require_declarations, verify_installed_lock, verify_extension_payload
    require_declarations('macos', ROOT)
    verify_installed_lock(ROOT / 'desktop/requirements-lock.txt')
    now = datetime.now()
    build_number = now.strftime('%Y%m%d%H%M%S')
    snapshot = source_snapshot()
    snapshot_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    build_dir = output / now.strftime('%Y%m%d-%H%M%S')
    build_dir.mkdir(parents=True, exist_ok=False)
    run(sys.executable, '-m', 'PyInstaller', '--noconfirm',
        '--distpath', str(build_dir / 'frozen'), '--workpath', str(build_dir / 'work'),
        str(ROOT / 'desktop/backend.spec'), cwd=ROOT)
    stage = build_dir / 'installer'
    app = stage / 'MinDock.app'
    contents = app / 'Contents'
    resources = contents / 'Resources'
    macos = contents / 'MacOS'
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    shutil.copytree(build_dir / 'frozen/mindock-backend', resources / 'backend')
    extension_payload = verify_extension_payload(resources / 'backend/_internal', 'macos')
    # The runtime hook exits before backend initialization: no user-data access.
    probe = json.loads(subprocess.check_output(
        [str(resources / 'backend/mindock-backend'), '--extension-probe'], text=True, timeout=60))
    (build_dir / 'extension-probe.json').write_text(json.dumps(probe, indent=2))
    asr_probe = json.loads(subprocess.check_output(
        [str(resources / 'backend/mindock-backend'), '--asr-probe'], text=True, timeout=120))
    (build_dir / 'asr-probe.json').write_text(json.dumps(asr_probe, indent=2))
    reader_probe = json.loads(subprocess.check_output(
        [str(resources / 'backend/mindock-backend'), '--reader-probe'], text=True, timeout=60))
    (build_dir / 'reader-probe.json').write_text(json.dumps(reader_probe, indent=2))
    swift = '/usr/bin/swiftc'
    target = 'arm64-apple-macos14.0'
    run(swift, '-O', '-target', target, str(ROOT / 'desktop/MinDock.swift'),
        '-o', str(macos / 'MinDock'), '-framework', 'AppKit', '-framework', 'WebKit')
    run(swift, '-O', '-target', target, str(ROOT / 'desktop/OCR.swift'),
        '-o', str(resources / 'mindock-ocr'), '-framework', 'AppKit', '-framework', 'Vision', '-framework', 'PDFKit')
    icons = build_dir / 'MinDock.iconset'
    icons.mkdir()
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            suffix = '@2x' if scale == 2 else ''
            run('/usr/bin/sips', '-z', str(size * scale), str(size * scale),
                str(ROOT / 'web/assets/mindock-mark.png'), '--out', str(icons / f'icon_{size}x{size}{suffix}.png'),
                stdout=subprocess.DEVNULL)
    run('/usr/bin/iconutil', '-c', 'icns', str(icons), '-o', str(resources / 'MinDock.icns'))
    info = {
        'CFBundleName': 'MinDock', 'CFBundleDisplayName': 'MinDock 思泊',
        'CFBundleIdentifier': 'local.mindock.desktop', 'CFBundleExecutable': 'MinDock',
        'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': version,
        'CFBundleVersion': build_number, 'CFBundleIconFile': 'MinDock.icns',
        'LSMinimumSystemVersion': '14.0', 'LSMultipleInstancesProhibited': True,
        'NSHighResolutionCapable': True, 'NSPrincipalClass': 'NSApplication',
        'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True,
                                  'NSAllowsArbitraryLoadsInWebContent': True},
        'NSDocumentsFolderUsageDescription': 'MinDock 读取你选择的资料并保存到本机知识空间。',
        'NSDownloadsFolderUsageDescription': 'MinDock 导入你选择的文件并导出日历、图谱和工作流。',
        'NSDesktopFolderUsageDescription': 'MinDock 导入你放在桌面并选择的资料。',
        'NSMicrophoneUsageDescription': 'MinDock 在你启动旁听时使用麦克风，将课堂声音转为本地笔记。',
    }
    with (contents / 'Info.plist').open('wb') as handle:
        plistlib.dump(info, handle)
    shutil.copy2(ROOT / 'desktop/INSTALL.txt', stage / '安装说明.txt')
    shutil.copy2(ROOT / 'LICENSE', resources / 'LICENSE.txt')
    # Dependency notices stay with the binary distribution.
    import importlib.metadata
    notices = []
    dependencies = {}
    for distribution in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        name = distribution.metadata['Name']
        dependencies[name] = distribution.version
        notices.append({'name': name, 'version': distribution.version,
                        'license': distribution.metadata.get('License-Expression') or distribution.metadata.get('License', ''),
                        'project_urls': distribution.metadata.get_all('Project-URL', [])})
        for file in distribution.files or []:
            if 'license' in str(file).lower() and Path(str(file)).name.lower().startswith(('license', 'copying')):
                destination = resources / 'licenses' / name / str(file).replace('/', '_')
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(distribution.locate_file(file), destination)
    (resources / 'third-party.json').write_text(json.dumps(notices, indent=2))
    for license_file in sorted((ROOT / 'web/assets').glob('*LICENSE*')) + sorted((ROOT / 'web/assets/vendor').glob('*LICENSE*')):
        destination = resources / 'licenses' / f'web-{license_file.name}'
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(license_file, destination)
    notices_file = ROOT / 'THIRD-PARTY-NOTICES.md'
    if notices_file.exists():
        shutil.copy2(notices_file, resources / 'THIRD-PARTY-NOTICES.md')
    for path in (Path(sys.base_prefix) / 'lib').glob('python*/LICENSE*'):
        destination = resources / 'licenses/Python' / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    metadata = {'version': version, 'build_number': build_number, 'source_sha256': snapshot_hash,
                'python': platform.python_version(), 'platform': platform.platform(),
                'swift': subprocess.check_output([swift, '--version'], text=True).strip(),
                'sdk': subprocess.check_output(['/usr/bin/xcrun', '--show-sdk-version'], text=True).strip(),
                'dependencies': dependencies, 'extensions': extension_payload, 'extension_probe': probe,
                'asr_probe': asr_probe, 'reader_probe': reader_probe}
    (resources / 'build-info.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    (build_dir / 'source-manifest.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    verify_private_files(resources)
    if source_snapshot() != snapshot:
        raise RuntimeError('Source changed during build; rebuild from a stable source snapshot.')
    sign_app(app, identity)
    if notary_profile:
        submission = build_dir / 'notary-app.zip'
        run('/usr/bin/ditto', '-c', '-k', '--keepParent', str(app), str(submission))
        run('/usr/bin/xcrun', 'notarytool', 'submit', str(submission), '--keychain-profile', notary_profile, '--wait')
        run('/usr/bin/xcrun', 'stapler', 'staple', str(app))
        run('/usr/bin/xcrun', 'stapler', 'validate', str(app))
        run('/usr/sbin/spctl', '--assess', '--type', 'execute', str(app))
    (stage / 'Applications').symlink_to('/Applications')
    dmg = output / f'MinDock-{version}-macOS-arm64-mcp-candidate-{build_dir.name}.dmg'
    run('/usr/bin/hdiutil', 'create', '-volname', 'MinDock 思泊', '-srcfolder', str(stage),
        '-format', 'UDZO', '-ov', str(dmg))
    if notary_profile:
        run('/usr/bin/codesign', '--sign', identity, '--timestamp', str(dmg))
        run('/usr/bin/xcrun', 'notarytool', 'submit', str(dmg), '--keychain-profile', notary_profile, '--wait')
        run('/usr/bin/xcrun', 'stapler', 'staple', str(dmg))
        run('/usr/bin/xcrun', 'stapler', 'validate', str(dmg))
    result = {**metadata, 'app': str(app), 'dmg': str(dmg),
              'sha256': hashlib.file_digest(dmg.open('rb'), 'sha256').hexdigest(),
              'architecture': 'arm64', 'minimum_macos': '14.0',
              'signature': 'ad-hoc' if identity == '-' else identity, 'notarized': bool(notary_profile)}
    (build_dir / 'build-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'output/macos')
    parser.add_argument('--identity', default='-')
    parser.add_argument('--notary-profile', help='Existing notarytool Keychain profile; never a password')
    args = parser.parse_args()
    build(args.output.resolve(), args.identity, args.notary_profile)
