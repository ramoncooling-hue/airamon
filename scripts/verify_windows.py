#!/usr/bin/env python3
"""Inspect the Windows payload; optionally run its real Python on Windows."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pe_machine(path):
    with path.open('rb') as stream:
        assert stream.read(2) == b'MZ', f'Not a Windows executable: {path.name}'
        stream.seek(0x3c)
        offset = struct.unpack('<I', stream.read(4))[0]
        stream.seek(offset)
        assert stream.read(4) == b'PE\0\0'
        return struct.unpack('<H', stream.read(2))[0]


def inspect_asr(backend):
    site = backend / 'python/Lib/site-packages'
    for module in ('faster_whisper', 'ctranslate2', 'onnxruntime', 'numpy',
                   'av', 'tokenizers', 'huggingface_hub'):
        assert (site / module / '__init__.py').is_file(), f'ASR module missing: {module}'
    vad = list((site / 'faster_whisper/assets').glob('silero_vad*.onnx'))
    assert vad and all(path.stat().st_size for path in vad), 'ASR VAD model missing'
    for module in ('ctranslate2', 'onnxruntime', 'numpy', 'av', 'tokenizers'):
        native = list((site / module).rglob('*.pyd'))
        assert native, f'ASR native module missing: {module}'
        for path in native + list((site / module).rglob('*.dll')):
            assert pe_machine(path) == 0x8664, f'ASR native binary is not Windows x64: {path.name}'
    for directory in ('av.libs', 'numpy.libs'):
        libraries = list((site / directory).glob('*.dll'))
        assert libraries, f'ASR sidecar DLLs missing: {directory}'
        for path in libraries:
            assert pe_machine(path) == 0x8664, f'ASR sidecar binary is not Windows x64: {path.name}'
    assert pe_machine(backend / 'bin/ffmpeg.exe') == 0x8664, 'ASR ffmpeg is not Windows x64'
    return {'target': 'windows', 'payload_files': 'present',
            'native_runtime_acceptance': 'not_checked', 'speech_models_bundled': False,
            'scope': 'ASR modules/native libraries/VAD/ffmpeg files only; runtime probe required on Windows'}


def inspect(unpacked, installer):
    resources = unpacked / 'resources'
    backend = resources / 'backend'
    python = backend / 'python'
    checks = []
    assert pe_machine(unpacked / 'MinDock.exe') == 0x8664
    assert pe_machine(python / 'python.exe') == 0x8664
    assert pe_machine(installer) in {0x14c, 0x8664}
    checks.append('Electron and bundled Python are Windows x64 PE; NSIS installer is PE')
    assert (resources / 'app.asar').stat().st_size > 10000
    assert (python / 'python312.zip').is_file()
    assert (python / 'python312._pth').read_text().splitlines() == ['python312.zip', '.', 'Lib/site-packages', '..', 'import site']
    checks.append('embedded Python import paths and Electron application payload present')
    for path in ('app/schema/019_ingest_jobs.sql', 'web/index.html', 'web/assets/cytoscape.min.js',
                 'seed/manifest.json', 'agents/laoba/system.md', 'desktop/backend.py', 'desktop/windows/ocr.py'):
        assert (backend / path).is_file(), path
    site = python / 'Lib/site-packages'
    assert len(list((site / 'rapidocr_onnxruntime').rglob('*.onnx'))) >= 3
    assert list(site.glob('onnxruntime/capi/*.dll'))
    assert list(site.glob('pypdfium2_raw/*.dll'))
    assert (site / 'tzdata/zoneinfo/Asia/Shanghai').is_file()
    assert (site / 'colorama/__init__.py').is_file()
    assert (site / 'pyreadline3/__init__.py').is_file()
    for name in ('msvcp140.dll', 'msvcp140_1.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
        assert pe_machine(python / name) == 0x8664
    checks.append('shared application, public seed, graph assets and offline OCR models included')
    from scripts.check_extension_packaging import verify_extension_payload
    verify_extension_payload(site, 'windows')
    asr = inspect_asr(backend)
    checks.append('MCP/sqlite-vec and Windows x64 ASR/PyAV/VAD/ffmpeg payload present')
    forbidden = [str(path.relative_to(backend)) for path in backend.rglob('*')
                 if path.name in {'banxue.db', 'settings.json', '.env'} or path.suffix in {'.dmg', '.icns'}]
    assert not forbidden, forbidden
    from scripts.build_macos import verify_private_files
    verify_private_files(backend)
    checks.append('payload excludes personal database, settings and locally configured credentials')
    return {'ok': True, 'inspection_checks': checks, 'asr': asr, 'installer': str(installer),
            'sha256': hashlib.sha256(installer.read_bytes()).hexdigest(),
            'windows_backend_verified': False,
            'windows_desktop_verified': False,
            'windows_installer_verified': False}


def exercise(unpacked):
    if sys.platform != 'win32':
        raise RuntimeError('Runtime verification must run on Windows; inspection is available on other hosts.')
    backend = unpacked / 'resources/backend'
    python = backend / 'python/python.exe'
    checks = []
    with tempfile.TemporaryDirectory(prefix='MinDock-Windows-验收-') as temporary:
        root = Path(temporary)
        descriptor = root / 'session.json'
        shutdown = root / 'shutdown'
        token = secrets.token_hex(32)
        env = {**os.environ, 'CYBERPAL_DATA_DIR': str(root), 'MINDOCK_SESSION_FILE': str(descriptor),
               'MINDOCK_SESSION_TOKEN': token, 'MINDOCK_SHUTDOWN_FILE': str(shutdown),
               'MINDOCK_PARENT_PID': str(os.getpid()), 'MINDOCK_BUNDLED_RUNTIME': '1',
               'MINDOCK_OCR': str(backend / 'desktop/windows/ocr.py'), 'MINDOCK_OCR_PYTHON': str(python),
               'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
        for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'MINDOCK_RUNTIME_LOCKED'):
            env.pop(key, None)
        command = [str(python), str(backend / 'desktop/backend.py')]
        asr_probe = subprocess.run([str(python), str(backend / 'desktop/windows/asr_probe.py')],
            env=env, capture_output=True, text=True, encoding='utf-8', check=True, timeout=120)
        assert json.loads(asr_probe.stdout)['ok']
        checks.append('actual bundled ASR: PyAV WAV decode, Silero VAD and ffmpeg/application decode')

        def start():
            descriptor.unlink(missing_ok=True)
            shutdown.unlink(missing_ok=True)
            log = (root / 'test.log').open('ab')
            process = subprocess.Popen(command, env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT)
            log.close()
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError((root / 'test.log').read_text(errors='replace')[-5000:])
                if descriptor.exists():
                    base = 'http://127.0.0.1:' + str(json.loads(descriptor.read_text())['port'])
                    try:
                        req = urllib.request.Request(base + '/api/health', headers={'X-MinDock-Session': token})
                        with urllib.request.urlopen(req, timeout=1) as response:
                            return process, base, json.load(response)
                    except OSError:
                        pass
                time.sleep(.15)
            process.terminate()
            process.wait(15)
            raise RuntimeError('Windows backend did not become ready')

        def stop(process):
            shutdown.write_text('stop')
            try:
                process.wait(20)
                assert process.returncode == 0, process.returncode
            except BaseException:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                raise

        process, base, health = start()
        first_base = base
        def request(path, body=None, method=None):
            payload = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=payload, method=method,
                headers={'X-MinDock-Session': token, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.load(response)
        try:
            assert health['tables']['courses'] >= 10
            try:
                urllib.request.urlopen(base + '/api/health')
                raise AssertionError('Missing session accepted')
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
            assert request('/api/runtime')['data_dir'] == str(root.resolve())
            checks.append('clean startup, user data separation and desktop session guard')
            course = 'Windows中文验收'
            request('/api/courses', {'name': course, 'nature': '选修课'})
            content = '# Opportunity cost\n\nOpportunity cost measures the next best alternative.\n'.encode()
            upload = request('/api/intake/upload?' + urllib.parse.urlencode({'course': course, 'filename': '课堂笔记.md', 'source_kind': 'class_note'}), content)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                job = request(f"/api/ingest/jobs/{upload['task_id']}")
                if job['status'] not in {'queued', 'running'}:
                    break
                time.sleep(.1)
            assert job['status'] in {'waiting_review', 'succeeded'}, job
            candidates = request('/api/sources?' + urllib.parse.urlencode({'course': course}))['candidates']
            candidate = next(item for item in candidates if item['status'] == 'pending')
            confirmed = request(f"/api/sources/candidates/{candidate['id']}/confirm", {})
            kp_id = confirmed['target_kp_id']
            assert request(f'/api/kp/{kp_id}')
            checks.append('Chinese paths, background extraction and confirmed knowledge persistence')
            cfg = request('/api/settings', {'fast': {'provider': 'custom', 'base_url': 'http://127.0.0.1:9999/v1', 'model': 'test', 'api_key': 'test-only-key'}}, 'PUT')
            assert cfg['configured']['fast'] and cfg['config']['fast']['api_key_set']
            assert 'test-only-key' not in json.dumps(request('/api/settings'))
            checks.append('settings save and credential masking without paid model calls')
            prompt = request('/api/hub/agent/laoba')['content'] + '\nWindows persistence check.\n'
            request('/api/hub/agent/laoba', {'content': prompt}, 'PATCH')
            from PIL import Image, ImageDraw, ImageFont
            image = Image.new('RGB', (1000, 200), 'white')
            font = ImageFont.truetype(str(Path(os.environ['WINDIR']) / 'Fonts/arial.ttf'), 48)
            ImageDraw.Draw(image).text((30, 50), 'MinDock OCR 2026', font=font, fill='black')
            image_path = root / 'ocr.png'
            image.save(image_path)
            recognized = subprocess.run([str(python), str(backend / 'desktop/windows/ocr.py'), '--image', str(image_path)],
                                        capture_output=True, text=True, encoding='utf-8', env=env, check=True, timeout=120).stdout
            assert 'MinDock' in recognized and '2026' in recognized, recognized
            image.save(root / 'scanned.pdf', 'PDF')
            recognized_pdf = subprocess.run([str(python), str(backend / 'desktop/windows/ocr.py'), '--pdf', str(root / 'scanned.pdf')],
                                            capture_output=True, text=True, encoding='utf-8', env=env, check=True, timeout=120).stdout
            assert '2026' in json.loads(recognized_pdf)[0]['text']
            checks.append('bundled offline image and scanned PDF OCR')
        finally:
            stop(process)
        process, base, health = start()
        try:
            assert first_base == base
            assert request('/api/hub/agent/laoba')['content'] == prompt
            assert request(f'/api/kp/{kp_id}')
            assert Path(upload['path']).read_bytes() == content
            checks.append('graceful stop and restart preserve source, knowledge and Hub edit')
        finally:
            stop(process)
        archive = root.parent / ('MinDock-test-' + secrets.token_hex(6) + '.zip')
        try:
            for operation in ('backup', 'inspect', 'restore'):
                result = subprocess.run(command + ['--maintenance', operation, '--archive', str(archive)],
                    env=env, capture_output=True, text=True, encoding='utf-8', check=True, timeout=120)
                assert json.loads(result.stdout)['ok']
            checks.append('actual bundled backup, archive inspection and restore')
        finally:
            archive.unlink(missing_ok=True)
    return checks


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('unpacked', type=Path)
    parser.add_argument('installer', type=Path)
    parser.add_argument('--runtime', action='store_true')
    args = parser.parse_args()
    result = inspect(args.unpacked.resolve(), args.installer.resolve())
    if args.runtime:
        result['runtime_checks'] = exercise(args.unpacked.resolve())
        result['windows_backend_verified'] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
