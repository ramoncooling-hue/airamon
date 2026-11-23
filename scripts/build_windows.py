#!/usr/bin/env python3
"""Cross-assemble a Windows x64 desktop using official Python and binary wheels."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time
import tempfile
import urllib.request
import zipfile
from datetime import datetime
from email.parser import BytesParser
import re

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_extension_packaging import pins, require_declarations
PYTHON_VERSION = '3.12.10'
PYTHON_URL = f'https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip'
ASR_PINS = {'faster-whisper': '1.2.1', 'ctranslate2': '4.8.3', 'av': '16.1.0',
            'onnxruntime': '1.31.0', 'numpy': '2.2.6', 'imageio-ffmpeg': '0.6.0',
            'tokenizers': '0.22.2', 'huggingface-hub': '0.36.0'}


def require_asr_declarations(lock):
    locked = pins(lock)
    missing = [name for name, version in ASR_PINS.items() if locked.get(name) != version]
    if missing:
        raise RuntimeError('Windows ASR dependency lock is missing or mismatched: ' + ', '.join(missing))


def collect_ffmpeg(site, stage):
    binaries = list((site / 'imageio_ffmpeg/binaries').glob('ffmpeg-win-*.exe'))
    if len(binaries) != 1:
        raise RuntimeError('Windows ASR requires exactly one bundled ffmpeg executable')
    target = stage / 'bin/ffmpeg.exe'
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binaries[0], target)
    return target


def project_version():
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    if not version:
        raise RuntimeError('VERSION is empty')
    return version


def assert_version_consistency():
    """Keep Windows app metadata aligned with the release version."""
    version = project_version()
    package = json.loads((ROOT / 'desktop/windows/package.json').read_text(encoding='utf-8'))
    lock = json.loads((ROOT / 'desktop/windows/package-lock.json').read_text(encoding='utf-8'))
    lock_root = lock.get('packages', {}).get('', {})
    actual = {
        'desktop/windows/package.json': package.get('version'),
        'desktop/windows/package-lock.json': lock.get('version'),
        'desktop/windows/package-lock.json#packages[""]': lock_root.get('version'),
    }
    mismatched = {name: value for name, value in actual.items() if value != version}
    if mismatched:
        details = ', '.join(f'{name}={value!r}' for name, value in mismatched.items())
        raise RuntimeError(f'Windows package version mismatch: expected {version!r}; {details}')
    return version


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def download(url, path, expected=None):
    if path.exists():
        if expected and digest(path) != expected:
            raise ValueError(f'Cached artifact checksum mismatch: {path.name}')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.partial')
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as response, temporary.open('wb') as output:
                shutil.copyfileobj(response, output)
            if expected and digest(temporary) != expected:
                raise ValueError(f'Download checksum mismatch: {path.name}')
            temporary.replace(path)
            return
        except OSError:
            temporary.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(1)


def run(*command, cwd=ROOT):
    if sys.platform == 'win32' and command[0] in {'npm', 'npx'}:
        command = (command[0] + '.cmd', *command[1:])
    subprocess.run(command, cwd=cwd, check=True)


def source_files():
    files = []
    for folder in ('app', 'web', 'seed/public', 'domains', 'agents', 'skills', 'docs/platform', 'scripts'):
        files.extend(p for p in (ROOT / folder).rglob('*') if p.is_file()
                     and '__pycache__' not in p.parts and p.suffix != '.pyc')
    files.extend(p for p in (ROOT / 'desktop').glob('*.py'))
    files.extend(p for p in (ROOT / 'desktop/windows').glob('*') if p.is_file())
    files.extend(ROOT / name for name in ('VERSION', 'LICENSE', 'requirements.txt', 'requirements-extensions.txt'))
    return sorted(set(files))


def snapshot():
    return {str(p.relative_to(ROOT)): digest(p) for p in source_files()}


def extract_wheel(wheel, destination):
    with zipfile.ZipFile(wheel) as archive:
        for entry in archive.infolist():
            parts = PurePosixPath(entry.filename).parts
            if not parts or '..' in parts or entry.filename.startswith('/'):
                raise ValueError('Unsafe wheel entry')
            if parts[0].endswith('.data'):
                if len(parts) < 3 or parts[1] not in {'purelib', 'platlib'}:
                    continue
                parts = parts[2:]
            target = destination.joinpath(*parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open('wb') as output:
                    shutil.copyfileobj(source, output)


def verify_wheel_lock(records, lock):
    expected = pins(lock)
    actual = {p['name']: p['version'] for p in records}
    hashes = {}
    for line in lock.read_text().splitlines():
        if '==' not in line or line.lstrip().startswith('#'):
            continue
        name = re.sub(r'[-_.]+', '-', line.split('==')[0]).lower()
        hashes[name] = re.findall(r'--hash=sha256:([a-f0-9]{64})', line)
    if actual != expected or len(records) != len(actual):
        raise RuntimeError('Windows wheel cache does not exactly match requirements-lock.txt')
    if any(p['sha256'] not in hashes.get(p['name'], []) for p in records):
        raise RuntimeError('Windows wheel hash does not match requirements-lock.txt')


def verify_wheel_dependencies(wheels):
    """Evaluate markers as Windows, including extras (pip cross-download uses host markers)."""
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    metadata = {}
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            entry = next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))
            meta = BytesParser().parsebytes(archive.read(entry))
        metadata[canonicalize_name(meta['Name'])] = meta
    env = default_environment()
    env.update(sys_platform='win32', platform_system='Windows', os_name='nt',
               platform_machine='AMD64', python_version='3.12', python_full_version=PYTHON_VERSION)
    pending = [(name, '') for name in metadata]
    visited = set()
    while pending:
        name, extra = pending.pop()
        if (name, extra) in visited:
            continue
        visited.add((name, extra))
        for raw in metadata[name].get_all('Requires-Dist', []):
            dep = Requirement(raw)
            if dep.marker and not dep.marker.evaluate({**env, 'extra': extra}):
                continue
            key = canonicalize_name(dep.name)
            if key not in metadata or not dep.specifier.contains(metadata[key]['Version']):
                raise RuntimeError(f'Windows dependency closure missing or incompatible: {name} -> {key}')
            pending.extend((key, item) for item in dep.extras)


def candidate_builder_config(output, evidence, version, stamp):
    """Override every path baked into the old desktop configuration for this candidate."""
    config = json.loads((ROOT / 'desktop/windows/package.json').read_text())['build']
    config['directories'] = {'output': str(evidence / 'installer'),
                             'buildResources': str(output / 'build-resources')}
    config['electronDist'] = str(output / 'downloads/electron-v44.7.0-win32-x64.zip')
    config['extraResources'] = [{'from': str(output / 'staging/backend'), 'to': 'backend'}]
    config['win']['icon'] = str(output / 'build-resources/MinDock.ico')
    config['nsis']['artifactName'] = f'MinDock-{version}-Windows-x64-mcp-candidate-{stamp}-Setup.${{ext}}'
    # Existing hook also embeds an old output path. Generate a scoped copy; keep source untouched.
    hook = (ROOT / 'desktop/windows/after-pack.cjs').read_text()
    old_icon = "path.resolve(__dirname,'../../output/windows/build-resources/MinDock.ico')"
    old_resedit = "require('resedit')"
    if hook.count(old_icon) != 1 or hook.count(old_resedit) != 1:
        raise RuntimeError('after-pack hook changed; review candidate path adaptation')
    hook = hook.replace(old_icon, json.dumps(config['win']['icon']))
    hook = hook.replace(old_resedit, 'require(' + json.dumps(str(ROOT / 'desktop/windows/node_modules/resedit')) + ')')
    hook_path = evidence / 'after-pack.cjs'
    hook_path.write_text(hook)
    config['afterPack'] = str(hook_path)
    config_path = evidence / 'electron-builder.json'
    config_path.write_text(json.dumps(config, indent=2))
    return config_path


def prepare(output, *, resolve=False, offline=False):
    output.mkdir(parents=True, exist_ok=True)
    downloads = output / 'downloads'
    python_zip = downloads / Path(PYTHON_URL).name
    artifacts = json.loads((ROOT / 'desktop/windows/runtime-artifacts.json').read_text())
    for artifact in artifacts.values():
        destination = downloads / artifact['filename']
        if offline and not destination.exists():
            raise FileNotFoundError(f'Offline build artifact missing: {destination.name}')
        download(artifact['url'], destination, artifact['sha256'])
    wheels = downloads / 'wheels-asr'
    requirements = ROOT / 'desktop/windows/requirements.txt'
    lock = ROOT / 'desktop/windows/requirements-lock.txt'
    if not resolve:
        require_declarations('windows', ROOT)
        require_asr_declarations(lock)
    index = ('--no-index', '--find-links', str(wheels)) if offline else ('--index-url', 'https://pypi.org/simple')
    if resolve or not lock.exists():
        run(sys.executable, '-m', 'pip', 'download', *index,
            '--dest', str(wheels), '--platform', 'win_amd64', '--python-version', '3.12',
            '--implementation', 'cp', '--abi', 'cp312', '--only-binary=:all:', '-r', str(requirements))
    else:
        run(sys.executable, '-m', 'pip', 'download', *index,
            '--dest', str(wheels), '--platform', 'win_amd64', '--python-version', '3.12',
            '--implementation', 'cp', '--abi', 'cp312', '--only-binary=:all:', '--no-deps',
            '--require-hashes', '-r', str(lock))
    package_records = []
    all_wheels = sorted(wheels.glob('*.whl'))
    seen = set()
    for wheel in all_wheels:
        with zipfile.ZipFile(wheel) as archive:
            entry = next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))
            meta = BytesParser().parsebytes(archive.read(entry))
        name = meta['Name'].lower().replace('_', '-')
        if name in seen:
            raise ValueError(f'Duplicate wheels for {name}; use a clean download directory')
        seen.add(name)
        package_records.append({'name': name, 'version': meta['Version'], 'wheel': wheel.name,
            'sha256': digest(wheel), 'license': meta.get('License-Expression') or meta.get('License', '')})
    if resolve or not lock.exists():
        lock.write_text('# Windows x64 CPython 3.12: all runtime wheels with exact hashes.\n' +
            '\n'.join(f"{p['name']}=={p['version']} --hash=sha256:{p['sha256']}" for p in package_records) + '\n')
    verify_wheel_lock(package_records, lock)
    verify_wheel_dependencies(all_wheels)
    require_asr_declarations(lock)
    stage = output / 'staging/backend'
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for folder in ('app', 'web', 'domains', 'agents', 'skills', 'docs/platform'):
        shutil.copytree(ROOT / folder, stage / folder, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copytree(ROOT / 'seed/public', stage / 'seed')
    (stage / 'desktop/windows').mkdir(parents=True)
    for file in (ROOT / 'desktop').glob('*.py'):
        shutil.copy2(file, stage / 'desktop' / file.name)
    for file in ('ocr.py', 'asr_probe.py'):
        shutil.copy2(ROOT / 'desktop/windows' / file, stage / 'desktop/windows' / file)
    for file in ('__init__.py', 'load_memory.py', 'migrate_runtime.py', 'backup_runtime.py', 'check_extension_packaging.py'):
        (stage / 'scripts').mkdir(exist_ok=True)
        shutil.copy2(ROOT / 'scripts' / file, stage / 'scripts' / file)
    for file in ('VERSION', 'LICENSE'):
        shutil.copy2(ROOT / file, stage / file)
    python_dir = stage / 'python'
    python_dir.mkdir()
    with zipfile.ZipFile(python_zip) as archive:
        archive.extractall(python_dir)
    with zipfile.ZipFile(downloads / artifacts['vclibs']['filename']) as archive:
        for name in ('msvcp140.dll', 'msvcp140_1.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
            (python_dir / name).write_bytes(archive.read(name))
    site = python_dir / 'Lib/site-packages'
    site.mkdir(parents=True)
    for wheel in all_wheels:
        extract_wheel(wheel, site)
    collect_ffmpeg(site, stage)
    (python_dir / 'python312._pth').write_text('python312.zip\n.\nLib/site-packages\n..\nimport site\n')
    # Some packages use .pth files to expose bundled native-library directories.
    # Embedded Python imports site explicitly; package data/models are retained.
    (stage / 'third-party.json').write_text(json.dumps(package_records, indent=2))
    (stage / 'python-distribution.json').write_text(json.dumps({'version': PYTHON_VERSION,
        'url': PYTHON_URL, 'sha256': digest(python_zip)}, indent=2))
    (stage / 'runtime-artifacts.json').write_text(json.dumps(artifacts, indent=2))
    licenses_dir = stage / 'licenses'
    licenses_dir.mkdir(exist_ok=True)
    for license_file in sorted((ROOT / 'web/assets').glob('*LICENSE*')) + sorted((ROOT / 'web/assets/vendor').glob('*LICENSE*')):
        shutil.copy2(license_file, licenses_dir / f'web-{license_file.name}')
    notices_md = ROOT / 'THIRD-PARTY-NOTICES.md'
    if notices_md.exists():
        shutil.copy2(notices_md, stage / 'THIRD-PARTY-NOTICES.md')
    (stage / 'THIRD-PARTY-NOTICES.txt').write_text(
        'MinDock Windows runtime components\n\n'
        'Python: PSF License, see python/LICENSE.txt.\n'
        'Electron/Chromium: see LICENSE.electron.txt and LICENSES.chromium.html in the application folder.\n'
        'Python dependencies: versions and licenses in third-party.json; package license files remain in Lib/site-packages.\n'
        'Web front-end components (Cytoscape/Lucide/markdown-it/DOMPurify): see licenses/web-*.txt.\n'
        'Microsoft Visual C++ desktop runtime DLLs: Copyright Microsoft Corporation.\n'
        'Source: https://aka.ms/Microsoft.VCLibs.x64.14.00.Desktop.appx\n'
        'Redistribution reference: https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files\n'
        'Microsoft components retain their own terms; the MinDock license does not relicense them.\n')
    shutil.copy2(lock, stage / 'requirements-lock.txt')
    model_files = list((site / 'rapidocr_onnxruntime').rglob('*.onnx'))
    if len(model_files) < 3:
        raise RuntimeError('Offline OCR models missing from wheel')
    from scripts.build_macos import verify_private_files
    verify_private_files(stage)
    from scripts.check_extension_packaging import verify_extension_payload
    require_declarations('windows', ROOT)
    extension_payload = verify_extension_payload(site, 'windows')
    (stage / 'extension-payload.json').write_text(json.dumps(extension_payload, indent=2))
    from scripts.verify_windows import inspect_asr
    (stage / 'asr-payload.json').write_text(json.dumps(inspect_asr(stage), indent=2))
    if sys.platform == 'win32':
        probe = json.loads(subprocess.check_output(
            [str(python_dir / 'python.exe'), str(stage / 'scripts/check_extension_packaging.py'),
             '--extension-probe'], text=True, timeout=60, cwd=stage))
        (stage / 'extension-probe.json').write_text(json.dumps(probe, indent=2))
        with tempfile.TemporaryDirectory(prefix='mindock-windows-asr-') as temporary:
            probe = subprocess.check_output(
                [str(python_dir / 'python.exe'), str(stage / 'desktop/windows/asr_probe.py')],
                text=True, timeout=120, cwd=stage,
                env={**os.environ, 'CYBERPAL_DATA_DIR': temporary, 'PYTHONUTF8': '1'})
            (stage / 'asr-probe.json').write_text(probe)
    from PIL import Image
    icons = output / 'build-resources'
    icons.mkdir(exist_ok=True)
    with Image.open(ROOT / 'web/assets/mindock-mark.png') as icon:
        icon.convert('RGBA').save(icons / 'MinDock.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
    return stage, package_records


def build(output, *, resolve=False, prepare_only=False, offline=False):
    version = assert_version_consistency()
    before = snapshot()
    stage, dependencies = prepare(output, resolve=resolve, offline=offline)
    after_prepare = snapshot()
    if resolve:
        before['desktop/windows/requirements-lock.txt'] = after_prepare['desktop/windows/requirements-lock.txt']
    if before != after_prepare:
        raise RuntimeError('Runtime source changed during preparation; rebuild before release')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    evidence = output / stamp
    evidence.mkdir(exist_ok=False)
    metadata = {'version': version, 'target': 'Windows 10/11 x64', 'python': PYTHON_VERSION,
        'electron': json.loads((ROOT / 'desktop/windows/package.json').read_text())['devDependencies']['electron'],
        'build_host': sys.platform, 'dependencies': dependencies,
        'source_sha256': hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
        'windows_backend_verified': False, 'windows_desktop_verified': False,
        'windows_installer_verified': False, 'signed': False}
    (stage / 'build-info.json').write_text(json.dumps(metadata, indent=2))
    (evidence / 'source-manifest.json').write_text(json.dumps(before, indent=2))
    if prepare_only:
        print(json.dumps({'stage': str(stage), 'evidence': str(evidence)}))
        return
    run('npm', 'ci', '--ignore-scripts', cwd=ROOT / 'desktop/windows')
    config = candidate_builder_config(output, evidence, version, stamp)
    run('npx', '--no-install', 'electron-builder', '--win', 'nsis', '--x64', '--publish', 'never',
        '--config', str(config),
        cwd=ROOT / 'desktop/windows')
    if snapshot() != before:
        raise RuntimeError('Runtime source changed during build; rebuild before release')
    installer = evidence / 'installer' / f'MinDock-{version}-Windows-x64-mcp-candidate-{stamp}-Setup.exe'
    if not installer.is_file():
        raise RuntimeError('Expected installer was not produced')
    final = output / installer.name
    if final.exists():
        raise FileExistsError('Candidate installer already exists')
    shutil.copy2(installer, final)
    portable = output / f'MinDock-{version}-Windows-x64-mcp-candidate-{stamp}-Portable.zip'
    with zipfile.ZipFile(portable, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        unpacked = evidence / 'installer/win-unpacked'
        for file in sorted(unpacked.rglob('*')):
            if file.is_file():
                archive.write(file, Path('MinDock') / file.relative_to(unpacked))
    metadata.update(installer=str(final), sha256=digest(final), portable=str(portable), portable_sha256=digest(portable))
    (evidence / 'build-result.json').write_text(json.dumps(metadata, indent=2))
    (evidence / 'SHA256SUMS.txt').write_text(f'{digest(final)}  {final.name}\n{digest(portable)}  {portable.name}\n')
    print(json.dumps({k: v for k, v in metadata.items() if k != 'dependencies'}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'output/windows')
    parser.add_argument('--resolve', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    build(args.output.resolve(), resolve=args.resolve, prepare_only=args.prepare_only, offline=args.offline)
