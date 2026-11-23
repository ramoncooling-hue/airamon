import asyncio
from contextlib import ExitStack
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from app.api import routes
from app.ingest import control, jobs
from app.maintenance import lock as portable_lock
from app.memory import db, sources


def wait_for(task_id, states=jobs.TERMINAL, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get(task_id)
        if job['status'] in states:
            return job
        time.sleep(0.02)
    raise AssertionError(jobs.get(task_id))


class IngestJobsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for key, value in {'DATA_DIR': self.root, 'APP_DIR': self.root / 'app',
                           'DB_PATH': self.root / 'app' / 'test.db'}.items():
            self.stack.enter_context(patch.object(db, key, value))
        self.stack.enter_context(patch.object(routes, 'STUDYPAL_DIR', self.root / 'StudyPal'))
        for key, value in {'_stop': Event(), '_wake': Event(), '_thread': None,
                           '_lease': None, '_accepting': True}.items():
            self.stack.enter_context(patch.object(jobs, key, value))
        db.apply_schema()
        with db.connect() as conn:
            conn.execute("INSERT INTO courses(name) VALUES('ingest-test')")
        conn.close()
        self.folder = self.root / 'StudyPal' / 'ingest-test' / 'inbox'
        self.folder.mkdir(parents=True)
        self.app = FastAPI()
        self.app.include_router(routes.router)
        self.app.get('/api/health')(lambda: {'status': 'ok', 'tables': db.table_counts()})
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.addCleanup(jobs.stop)

    def material(self, name='notes.md', content='# Cost\nMarginal cost.\n'):
        path = self.folder / name
        path.write_text(content)
        return path

    def test_scan_without_course_and_invalid_course(self):
        self.material()
        with patch.object(routes.settings_store, 'read_raw', return_value={}):
            response = self.client.post('/api/sources/scan')
            self.assertEqual(response.status_code, 200)
            task_id = response.json()['ingest-test']['files'][0]['task_id']
            again = self.client.post('/api/sources/scan?course=ingest-test').json()
            self.assertEqual(again['ingest-test']['files'][0]['task_id'], task_id)
            self.assertEqual(self.client.post('/api/sources/scan?course=missing').status_code, 404)
        jobs.run_one()
        self.assertEqual(jobs.get(task_id)['status'], 'waiting_review')

    def test_upload_returns_before_extraction_and_polling_retains_review_contract(self):
        with patch.object(sources, 'extract_structured', wraps=sources.extract_structured) as extract:
            response = self.client.post('/api/intake/upload?course=ingest-test&filename=notes.md&source_kind=class_note',
                                        content=b'# Cost\nMarginal cost.')
            self.assertEqual(response.status_code, 200)
            task_id = response.json()['task_id']
            extract.assert_not_called()
            self.assertEqual(jobs.get(task_id)['status'], 'queued')
            jobs.start()
            self.assertEqual(jobs.get(task_id)['status'], 'interrupted')
            jobs.retry(task_id)
            job = wait_for(task_id)
        self.assertEqual(job['status'], 'waiting_review')
        self.assertTrue(job['source_id'])
        self.assertEqual(self.client.get(f'/api/ingest/jobs/{task_id}').json()['task_id'], task_id)
        self.assertEqual(len(self.client.get('/api/ingest/jobs?course=ingest-test').json()), 1)
        self.assertEqual(self.client.get('/api/ingest/jobs?workspace=missing').json(), [])
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM kp').fetchone()[0], 0)
        conn.close()
        candidate = sources.candidates('ingest-test')[0]
        confirmed = sources.confirm_candidate(candidate['id'])
        self.assertEqual(confirmed['status'], 'confirmed')
        self.assertIn(confirmed['target_kp_id'], {node['id'] for node in routes.graph('ingest-test')['nodes']})
        self.assertGreaterEqual(sum(row['total'] for row in routes.mastery('ingest-test')['rows']), 1)
        self.assertEqual(sources.search('Marginal', 'ingest-test')[0]['source'], job['path'])
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT source_kind FROM source_files').fetchone()[0], 'class_note')
            self.assertGreater(conn.execute('SELECT COUNT(*) FROM kp_evidence').fetchone()[0], 0)
        conn.close()

    def test_hash_dedupe_and_worker_serialization(self):
        entered, release = Event(), Event()
        original = sources.extract_structured

        def slow(path):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(path)

        first = jobs.enqueue('ingest-test', self.material(), self.folder)
        duplicate = jobs.enqueue('ingest-test', self.material('copy.md'), self.folder)
        self.assertEqual(first['id'], duplicate['id'])
        with patch.object(sources, 'extract_structured', side_effect=slow) as extract:
            jobs.start()
            self.assertEqual(jobs.get(first['id'])['status'], 'interrupted')
            jobs.retry(first['id'])
            self.assertTrue(entered.wait(3))
            try:
                self.assertFalse(jobs.run_one())
                scan = jobs.scan('ingest-test', [self.folder])
                self.assertEqual({f['task_id'] for f in scan['files']}, {first['id']})
            finally:
                release.set()
            self.assertEqual(wait_for(first['id'])['status'], 'waiting_review')
            self.assertEqual(extract.call_count, 1)

    def test_upgrade_adopts_existing_index_without_extracting_or_rewriting_candidates(self):
        path = self.material()
        sources.scan_course('ingest-test', [self.folder])
        before = sources.candidates('ingest-test')
        with patch.object(sources, 'extract_structured', side_effect=AssertionError('must not re-OCR')):
            task = jobs.enqueue('ingest-test', path, self.folder)
            self.assertEqual(task['status'], 'waiting_review')
            self.assertFalse(jobs.run_one())
        self.assertEqual(sources.candidates('ingest-test'), before)
        sources.confirm_candidate(before[0]['id'])
        self.assertEqual(jobs.get(task['id'])['status'], 'succeeded')

    def test_same_path_new_version_and_reversion_are_indexed_with_classification(self):
        path = self.material()
        task = jobs.enqueue('ingest-test', path, self.folder, source_kind='class_note')
        original = sources.extract_structured

        def classified_extract(path):
            self.assertEqual(sources.status('ingest-test')[0]['source_kind'], 'class_note')
            return original(path)

        with patch.object(sources, 'extract_structured', side_effect=classified_extract):
            jobs.run_one()
        old_content = path.read_text()
        path.write_text('# Revenue\nRevenue grows.')
        updated = jobs.enqueue('ingest-test', path, self.folder)
        self.assertNotEqual(updated['id'], task['id'])
        jobs.run_one()
        self.assertEqual(sources.candidates('ingest-test')[0]['label'], 'Revenue')
        path.write_text(old_content)
        reverted = jobs.enqueue('ingest-test', path, self.folder)
        self.assertEqual(reverted['status'], 'queued')
        jobs.run_one()
        self.assertEqual(sources.candidates('ingest-test')[0]['label'], 'Cost')

    def test_duplicate_upload_returns_canonical_material_and_does_not_reindex(self):
        url = '/api/intake/upload?course=ingest-test&filename='
        first = self.client.post(url+'one.md', content=b'# Cost\nText').json()
        jobs.run_one()
        second = self.client.post(url+'two.md', content=b'# Cost\nText').json()
        self.assertTrue(second['deduplicated'])
        self.assertEqual(second['canonical_path'], first['path'])
        self.assertEqual(second['task_id'], first['task_id'])
        self.assertTrue(Path(second['path']).is_file())
        self.assertFalse(jobs.run_one())

    def test_duplicate_rebinds_obsolete_or_deleted_canonical(self):
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                content = f'# Original {deleted}\nVersion A'
                path = self.material(f'one-{deleted}.md', content)
                original = jobs.enqueue('ingest-test', path, self.folder)
                jobs.run_one()
                if deleted:
                    path.unlink()
                else:
                    path.write_text('# Replacement\nVersion B')
                    jobs.enqueue('ingest-test', path, self.folder)
                    jobs.run_one()
                replacement = self.material(f'two-{deleted}.md', content)
                rebound = jobs.enqueue('ingest-test', replacement, self.folder)
                self.assertEqual(rebound['path'], str(replacement.resolve()))
                self.assertEqual(rebound['status'], 'queued')
                jobs.run_one()
                done = jobs.get(rebound['id'])
                self.assertEqual(done['sha256'], sources.file_digest(Path(done['path'])))
                self.assertEqual(done['status'], 'waiting_review')
                with db.connect() as conn:
                    source = conn.execute('SELECT * FROM source_files WHERE id=?', (done['source_id'],)).fetchone()
                    self.assertEqual(source['path'], str(replacement.resolve()))
                    self.assertEqual(source['sha256'], done['sha256'])
                conn.close()

    def test_publication_and_registration_exclude_background_scan(self):
        published, scan_started, scan_done = Event(), Event(), Event()
        original = routes._publish_upload
        errors = []

        def background():
            try:
                self.assertTrue(published.wait(3))
                scan_started.set()
                jobs.scan('ingest-test', [self.folder])
                jobs.run_one()
            except BaseException as exc:
                errors.append(exc)
            finally:
                scan_done.set()

        def publish(*args):
            path = original(*args)
            published.set()
            self.assertTrue(scan_started.wait(3))
            self.assertFalse(scan_done.wait(.15), 'scanner entered before explicit metadata registration')
            return path

        thread = Thread(target=background)
        thread.start()
        try:
            with patch.object(routes, '_publish_upload', side_effect=publish):
                result = self.client.post('/api/intake/upload?course=ingest-test&filename=catalog.md&source_kind=course_catalog',
                                          content=b'# Catalog\nChapter 1').json()
        finally:
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        task = jobs.get(result['task_id'])
        self.assertEqual(task['source_kind'], 'course_catalog')
        self.assertEqual(task['source_role'], 'catalog')
        source = sources.status('ingest-test')[0]
        self.assertEqual(source['source_kind'], 'course_catalog')
        self.assertEqual(source['catalog_status'], 'candidate')

    def test_restart_does_not_execute_previous_queued_job(self):
        task = jobs.enqueue('ingest-test', self.material(), self.folder)
        with patch.object(sources, 'extract_structured') as extract:
            jobs.start()
            self.assertEqual(jobs.get(task['id'])['status'], 'interrupted')
            extract.assert_not_called()
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT status FROM ingest_runs WHERE id=?',
                                         (task['ingest_run_id'],)).fetchone()[0], 'interrupted')
        conn.close()

    def test_retry_is_explicit_and_does_not_repeat_confirmed_actions(self):
        path = self.material()
        task = jobs.enqueue('ingest-test', path, self.folder)
        with patch.object(sources, 'extract_structured', side_effect=ValueError('extract failed')):
            jobs.run_one()
        self.assertEqual(jobs.get(task['id'])['status'], 'failed')
        jobs.scan('ingest-test', [self.folder])
        self.assertEqual(jobs.get(task['id'])['status'], 'failed')
        self.client.post(f"/api/ingest/jobs/{task['id']}/retry")
        jobs.run_one()
        self.assertEqual(jobs.get(task['id'])['retry_count'], 1)
        sources.confirm_candidate(sources.candidates('ingest-test')[0]['id'])
        jobs.scan('ingest-test', [self.folder])
        self.assertFalse(jobs.run_one())
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM kp').fetchone()[0], 1)
        conn.close()

    def test_restart_interrupts_running_without_automatic_retry(self):
        task = jobs.enqueue('ingest-test', self.material(), self.folder)
        jobs._update(task['id'], status='running')
        jobs.start()
        self.assertEqual(jobs.get(task['id'])['status'], 'interrupted')
        jobs.stop()
        jobs.start()
        self.assertEqual(jobs.get(task['id'])['status'], 'interrupted')
        self.assertEqual(jobs.get(task['id'])['retry_count'], 0)
        jobs.retry(task['id'])
        self.assertEqual(wait_for(task['id'])['status'], 'waiting_review')

    def test_cancel_queued_and_stop_leave_persistent_interrupted_tasks(self):
        task = jobs.enqueue('ingest-test', self.material(), self.folder)
        self.assertEqual(self.client.post(f"/api/ingest/jobs/{task['id']}/cancel").json()['status'], 'cancelled')
        self.assertFalse(jobs.run_one())
        jobs.retry(task['id'])
        jobs.stop()
        self.assertEqual(jobs.get(task['id'])['status'], 'interrupted')
        with self.assertRaises(ValueError):
            jobs.enqueue('ingest-test', self.material('later.md'), self.folder)

    def test_native_child_progress_cancel_and_shutdown_reap_child(self):
        executable = self.root / 'ocr.py'
        executable.write_text('import sys,time\n'
                              'print(\'{"event":"progress","page":1,"total":4}\', file=sys.stderr, flush=True)\n'
                              'time.sleep(60)\nprint("recognized text")\n')
        children = []
        real_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            return proc

        def extract(command, **kwargs):
            return control.run([sys.executable, *command], **kwargs)

        with patch.object(sources, '_native_ocr', return_value=str(executable)), \
                patch.object(sources, 'run_extractor', side_effect=extract), \
                patch.object(control.subprocess, 'Popen', side_effect=spawn), \
                patch.object(control.os, 'pread', None, create=True):
            task = jobs.enqueue('ingest-test', self.material('image.png'), self.folder)
            jobs.start()
            self.assertEqual(jobs.get(task['id'])['status'], 'interrupted')
            jobs.retry(task['id'])
            deadline = time.monotonic() + 4
            while jobs.get(task['id'])['pages_done'] != 1 and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(jobs.get(task['id'])['pages_total'], 4)
            jobs.cancel(task['id'])
            self.assertEqual(wait_for(task['id'])['status'], 'cancelled')
            self.assertIsNotNone(children[0].returncode)
            jobs.retry(task['id'])
            deadline = time.monotonic() + 4
            while len(children) < 2 and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(len(children), 2)
            jobs.stop()
            self.assertEqual(jobs.get(task['id'])['status'], 'interrupted')
            self.assertIsNotNone(children[1].returncode)

    def probe_worker_lease(self):
        return subprocess.run([sys.executable, '-c',
            'import sys\nfrom app.maintenance.lock import _acquire, _release\n'
            'with open(sys.argv[1], "a+b") as lease:\n'
            '    try: _acquire(lease.fileno())\n'
            '    except BlockingIOError: sys.exit(73)\n'
            '    _release(lease.fileno())\n',
            str(db.DB_PATH) + '.ingest.lock'], capture_output=True, text=True, timeout=10)

    def test_worker_lease_excludes_another_process_until_stopped(self):
        jobs.start()
        self.assertEqual(self.probe_worker_lease().returncode, 73)
        jobs.stop()
        result = self.probe_worker_lease()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(Path(str(db.DB_PATH) + '.ingest.lock').is_file())

    def test_second_worker_cannot_recover_first_workers_running_job(self):
        task = jobs.enqueue('ingest-test', self.material(), self.folder)
        jobs.start()
        jobs._update(task['id'], status='running')
        code = (
            'import sys\nfrom pathlib import Path\nfrom app.memory import db\n'
            'from app.ingest import jobs\n'
            'db.DB_PATH = Path(sys.argv[1])\n'
            'try: jobs.start()\n'
            'except RuntimeError: sys.exit(73)\n'
            'jobs.stop()\n'
        )
        result = subprocess.run([sys.executable, '-c', code, str(db.DB_PATH)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual(jobs.get(task['id'])['status'], 'running')

    def test_worker_lease_uses_windows_primitives(self):
        from unittest.mock import Mock
        windows = Mock(LK_NBLCK=1, LK_UNLCK=0)
        with patch.object(portable_lock, 'fcntl', None), \
                patch.object(portable_lock, 'msvcrt', windows, create=True):
            jobs.start()
            windows.locking.assert_called_once_with(jobs._lease.fileno(), 1, 1)
            fd = jobs._lease.fileno()
            self.assertEqual(os.fstat(fd).st_size, 1)
            jobs.stop()
            self.assertEqual(windows.locking.call_args.args, (fd, 0, 1))

    def test_worker_releases_lease_on_connection_or_thread_start_failure(self):
        for target in ('_connect', 'Thread.start'):
            with self.subTest(target=target):
                failure = patch.object(jobs, '_connect', side_effect=ValueError('startup failed')) if target == '_connect' else \
                    patch.object(Thread, 'start', side_effect=ValueError('startup failed'))
                with failure, self.assertRaisesRegex(ValueError, 'startup failed'):
                    jobs.start()
                result = self.probe_worker_lease()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNone(jobs._lease)
                self.assertIsNone(jobs._thread)
                jobs.start()
                jobs.stop()

    def test_120mb_pdf_stays_archived_without_parser(self):
        path = self.folder / 'large.pdf'
        with path.open('wb') as stream:
            stream.truncate(120 * 1024 * 1024)
        task = jobs.enqueue('ingest-test', path, self.folder)
        with patch.object(sources, 'extract_structured') as extract:
            jobs.run_one()
            extract.assert_not_called()
        job = jobs.get(task['id'])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('100 MB', job['error'])
        self.assertEqual(path.stat().st_size, 120 * 1024 * 1024)
        self.assertEqual(sources.status('ingest-test')[0]['status'], 'ocr_required')

    def test_empty_oversized_and_disk_full_uploads_leave_no_partial_file(self):
        url = '/api/intake/upload?course=ingest-test&filename=broken.md'
        self.assertEqual(self.client.post(url, content=b'').status_code, 400)
        response = self.client.post(url, content=b'x', headers={'content-length': str(200 * 1024 * 1024 + 1)})
        self.assertEqual(response.status_code, 413)
        with patch.object(routes, '_write_upload_chunk', side_effect=OSError(errno.ENOSPC, 'No space')):
            with self.assertRaises(OSError):
                self.client.post(url, content=b'hello')
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertEqual(jobs.list_jobs(), [])
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ingest_runs WHERE status='running'").fetchone()[0], 0)
        conn.close()

    def test_health_requests_remain_responsive_while_extraction_runs(self):
        original = sources.extract_structured
        entered = Event()

        def slow(path):
            entered.set()
            time.sleep(1)
            return original(path)

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test') as client:
                start = time.monotonic()
                upload = await client.post('/api/intake/upload?course=ingest-test&filename=slow.md', content=b'# Cost\nText')
                self.assertLess(time.monotonic() - start, .5)
                task_id = upload.json()['task_id']
                deadline = time.monotonic() + 3
                while not entered.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                latencies = []
                for _ in range(30):
                    start = time.monotonic()
                    self.assertEqual((await client.get('/api/health')).status_code, 200)
                    latencies.append(time.monotonic() - start)
                    await asyncio.sleep(.01)
                self.assertLess(sorted(latencies)[28], .5)
                print(f'health during ingest: n=30 p95={sorted(latencies)[28]*1000:.1f}ms')
                return task_id

        with patch.object(sources, 'extract_structured', side_effect=slow):
            jobs.start()
            task_id = asyncio.run(scenario())
            self.assertEqual(wait_for(task_id)['status'], 'waiting_review')

    def test_dev_lifetime_lock_is_released_after_shutdown_and_failed_startup(self):
        from app import main
        from app.maintenance.lock import runtime_lock, RuntimeBusyError

        def attempt_lock(errors):
            try:
                with runtime_lock(self.root):
                    pass
            except Exception as exc:
                errors.append(exc)

        with patch.dict(os.environ, {'MINDOCK_RUNTIME_LOCKED': ''}), \
             patch.object(main, 'scan_all_sources', return_value={}), \
             patch.object(main, 'run_agent_triggers', return_value=None):
            main.startup()
            try:
                errors = []
                thread = Thread(target=attempt_lock, args=(errors,))
                thread.start()
                thread.join()
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], RuntimeBusyError)
            finally:
                main.shutdown()
            self.assertIsNone(main._scan_thread)
            errors = []
            thread = Thread(target=attempt_lock, args=(errors,))
            thread.start()
            thread.join()
            self.assertEqual(errors, [])
            with patch.object(main, 'apply_schema', side_effect=ValueError('migration failed')):
                with self.assertRaisesRegex(ValueError, 'migration failed'):
                    main.startup()
            self.assertIsNone(main._runtime_context)
            errors = []
            thread = Thread(target=attempt_lock, args=(errors,))
            thread.start()
            thread.join()
            self.assertEqual(errors, [])
