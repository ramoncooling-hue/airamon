#!/usr/bin/env python3
"""Exercise the actual frozen backend with isolated writable data."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def verify(app: Path) -> dict:
    resources = app.resolve() / 'Contents/Resources'
    backend = resources / 'backend/mindock-backend'
    with tempfile.TemporaryDirectory(prefix='mindock-install-test-') as tmp:
        # macOS exposes /var as a symlink to /private/var. The runtime
        # settings guard intentionally rejects symlinked parents, so the
        # acceptance harness must pass the canonical writable path.
        data = Path(tmp).resolve()
        token = secrets.token_hex(32)
        descriptor = data / 'session.json'
        env = {**os.environ, 'CYBERPAL_DATA_DIR': str(data),
               'MINDOCK_SESSION_FILE': str(descriptor), 'MINDOCK_SESSION_TOKEN': token,
               'MINDOCK_OCR': str(resources / 'mindock-ocr'),
               'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'}
        for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'):
            env.pop(key, None)
        log = data / 'test.log'
        checks = []

        def start():
            descriptor.unlink(missing_ok=True)
            process = subprocess.Popen([str(backend)], env=env, cwd='/tmp',
                                       stdout=log.open('ab'), stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(log.read_text(errors='replace')[-6000:])
                if descriptor.exists():
                    base = 'http://127.0.0.1:' + str(json.loads(descriptor.read_text())['port'])
                    try:
                        request = urllib.request.Request(base + '/api/health', headers={'X-MinDock-Session': token})
                        with urllib.request.urlopen(request, timeout=1) as response:
                            health = json.load(response)
                        return process, base, health
                    except (OSError, ValueError):
                        pass
                time.sleep(0.2)
            process.terminate()
            process.wait(timeout=15)
            raise RuntimeError('Frozen backend did not become ready: ' + log.read_text()[-6000:])

        def stop(process):
            process.terminate()
            process.wait(timeout=15)
            assert process.returncode in (0, -signal.SIGTERM), process.returncode

        process, base, health = start()
        first_base = base
        try:
            assert health['tables']['courses'] >= 10, health
            checks.append('clean first launch with public seed and no external Python')

            def request(path, body=None, method=None, raw=False):
                payload = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
                req = urllib.request.Request(base + path, data=payload, method=method,
                    headers={'X-MinDock-Session': token, 'Content-Type': 'application/octet-stream' if raw else 'application/json'})
                with urllib.request.urlopen(req, timeout=90) as response:
                    return response.read() if raw else json.load(response)

            runtime = request('/api/runtime')
            assert runtime['data_dir'] == str(data.resolve()), runtime
            checks.append('runtime data outside application bundle')
            try:
                urllib.request.urlopen(base + '/api/health')
                raise AssertionError('Unauthenticated backend request accepted')
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
            checks.append('loopback session guard')
            html = request('/', raw=True)
            assert b'cytoscape.min.js' in html
            assert b'StudyPal' in html
            assert request('/assets/cytoscape.min.js', raw=True)
            checks.append('frontend and local graph assets')
            from PIL import Image, ImageDraw, ImageFont
            image = Image.new('RGB', (1200, 200), 'white')
            ImageDraw.Draw(image).text((40, 50), 'MinDock OCR verification 2026',
                font=ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 52), fill='black')
            image_path = data / 'ocr-fixture.png'
            image.save(image_path)
            recognized = subprocess.run([str(resources / 'mindock-ocr'), '--image', str(image_path)],
                                         capture_output=True, text=True, check=True, timeout=60).stdout
            assert 'MinDock' in recognized and '2026' in recognized, recognized
            pdf_path = data / 'scanned-fixture.pdf'
            image.save(pdf_path, 'PDF')
            recognized_pdf = subprocess.run([str(resources / 'mindock-ocr'), '--pdf', str(pdf_path)],
                                             capture_output=True, text=True, check=True, timeout=60).stdout
            assert 'MinDock' in json.loads(recognized_pdf)[0]['text']
            checks.append('bundled offline image and scanned PDF OCR')
            request('/api/courses', {'name': 'Desktop verification', 'nature': '选修课'})
            content = '# Desktop knowledge\n\n## Opportunity cost\n\nOpportunity cost measures the value of the best alternative forgone.\n'.encode()
            query = urllib.parse.urlencode({'course': 'Desktop verification', 'filename': 'notes.md', 'source_kind': 'class_note'})
            uploaded = request('/api/intake/upload?' + query, content)
            material = Path(uploaded['path'])
            assert material.is_relative_to(data.resolve()) and material.read_bytes() == content
            assert uploaded['task_id'], uploaded
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                task = request(f"/api/ingest/jobs/{uploaded['task_id']}")
                if task['status'] not in {'queued', 'running'}:
                    break
                time.sleep(0.1)
            assert task['status'] in {'waiting_review', 'succeeded'}, task
            checks.append('material upload returns persistent job and indexes in background')
            sources = request('/api/sources?' + urllib.parse.urlencode({'course': 'Desktop verification'}))
            assert sources, sources
            candidates = sources['candidates']
            pending = [candidate for candidate in candidates if candidate['status'] == 'pending']
            assert pending, candidates
            confirmed = request(f"/api/sources/candidates/{pending[0]['id']}/confirm", {})
            kp_id = confirmed['target_kp_id']
            assert request(f'/api/kp/{kp_id}')
            checks.append('material candidate approval creates persistent knowledge with source')
            assert isinstance(request('/api/sources/scan', {}), dict)
            checks.append('all-course scan API succeeds')
            hub = request('/api/hub')
            assert len(hub['agents']) >= 4 and hub['skills']
            assert (data / 'app/hub/agents/laoba/system.md').is_file()
            detail = request('/api/hub/agent/laoba')
            edited_prompt = detail['content'] + '\nDesktop persistent edit verification.\n'
            request('/api/hub/agent/laoba', {'content': edited_prompt}, method='PATCH')
            assert request('/api/hub/agent/laoba')['content'] == edited_prompt
            checks.append('Agent prompt edit persisted in writable runtime copy')
            event = request('/api/events', {'title': 'Desktop reminder verification', 'date': '2026-10-10'})
            assert event
            assert b'BEGIN:VCALENDAR' in request('/api/calendar/export.ics?workspace=studypal', raw=True)
            workflow = request('/api/workflows', {'key': 'desktop-test', 'name': 'Desktop verification'})
            request('/api/workflows/desktop-test/steps', {'key': 'input', 'kind': 'input', 'name': 'Input'})
            request('/api/workflows/desktop-test/steps', {'key': 'review', 'kind': 'approval', 'name': 'Review'})
            run = request('/api/workflows/desktop-test/runs', {'input_data': {'question': 'A local test'}})
            assert run['status'] == 'waiting_approval', run
            run = request(f"/api/workflow-runs/{run['id']}/approve", {'decision': True})
            assert run['status'] == 'success', run
            checks.append('calendar write/export and actual workflow input-approval execution')
            request('/api/agent/profile', {'active_workspace_key': 'lifepal'}, method='PUT')
            assert request('/api/seed/refresh', {})['ok']
            assert any(course['name'] == 'Desktop verification' for course in request('/api/courses'))
            checks.append('frozen seed refresh preserves personal additions')
        finally:
            stop(process)
        checks.append('graceful backend shutdown')
        process, base, health = start()
        try:
            assert base == first_base
            assert material.read_bytes() == content
            assert health['tables']['courses'] >= 11
            assert request('/api/hub/agent/laoba')['content'] == edited_prompt
            assert request(f"/api/workflow-runs/{run['id']}")['status'] == 'success'
            checks.append('database, materials and UI origin persist after restart')
        finally:
            stop(process)
        with tempfile.TemporaryDirectory(prefix='mindock-recovery-test-') as recovery_tmp:
            archive = Path(recovery_tmp) / 'personal-backup.zip'
            recovered = Path(recovery_tmp) / 'restored'
            def maintenance(action, root, archive_path):
                result = subprocess.run([str(backend), '--maintenance', action, '--archive', str(archive_path)],
                    env={**env, 'CYBERPAL_DATA_DIR': str(root)}, cwd='/tmp', capture_output=True, text=True, timeout=120)
                assert result.returncode == 0, result.stderr[-1500:]
                return json.loads(result.stdout)
            assert maintenance('backup', data, archive)['ok']
            summary = maintenance('inspect', data, archive)
            assert summary['file_count'] > 0 and summary['sha256']
            marker = data / 'after-backup.txt'
            marker.write_text('Only present after backup', encoding='utf-8')
            result = maintenance('restore', data, archive)
            assert result['ok'] and not marker.exists()
            assert (Path(result['recovery_dir']) / marker.name).is_file()
            checks.append('replacement restore preserves previous payload and archive preview is available')
            assert maintenance('restore', recovered, archive)['ok']
            import sqlite3
            with sqlite3.connect(recovered / 'app/banxue.db') as connection:
                assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                assert connection.execute('SELECT name FROM kp WHERE id=?', (kp_id,)).fetchone()
                restored_path = connection.execute('SELECT path FROM source_files WHERE course_id=(SELECT id FROM courses WHERE name=?)', ('Desktop verification',)).fetchone()[0]
            assert Path(restored_path).is_relative_to(recovered.resolve())
            assert Path(restored_path).read_bytes() == content
            assert (recovered / 'app/hub/agents/laoba/system.md').read_text() == edited_prompt
            checks.append('frozen offline backup and restore validate data, Hub and relocated source paths')
        occupied = socket.socket()
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(('127.0.0.1', urllib.parse.urlparse(first_base).port))
        occupied.listen()
        try:
            process, base, health = start()
            try:
                assert base != first_base
                assert request('/api/agent/profile')['active_workspace_key'] == 'lifepal'
                assert request(f'/api/kp/{kp_id}')
            finally:
                stop(process)
        finally:
            occupied.close()
        checks.append('port collision chooses a new port and preserves workspace after restore')
        forbidden = [str(path) for path in resources.rglob('*') if path.name in {'settings.json', 'banxue.db', '.env'}]
        assert not forbidden, forbidden
        checks.append('no personal database or model settings in bundle')
        return {'ok': True, 'app': str(app.resolve()), 'checks': checks}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('app', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.app), ensure_ascii=False, indent=2))
