"""T2.4/O-DATA-5: catalog navigation needs an explicit active selection, and
ingest runs never stay running without a live worker heartbeat.
"""

import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from app.ingest import jobs
from app.memory import db, repo, sources

CATALOG_EVIDENCE = [
    {'locator_type': 'page', 'locator_start': 1,
     'text': '第1章 需求 学习目的\n1.1 需求曲线'},
]


class CatalogSelectionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(db, 'APP_DIR', self.root / 'app'))
        self.stack.enter_context(patch.object(db, 'DB_PATH', self.root / 'app' / 'test.db'))
        db.apply_schema()
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES('测试课程')")
                self.course_id = conn.execute(
                    'SELECT id FROM courses WHERE name=?', ('测试课程',)).fetchone()[0]
        finally:
            conn.close()

    def _add_source(self, *, path: str, catalog_status: str, status: str = 'indexed'):
        conn = db.connect()
        try:
            with conn:
                source_id = conn.execute(
                    """INSERT INTO source_files
                           (course_id,path,sha256,status,source_kind,source_role,catalog_status)
                       VALUES(?,?,?,?,'course_catalog','catalog',?)""",
                    (self.course_id, path, path, status, catalog_status)).lastrowid
            return source_id
        finally:
            conn.close()

    def _count(self, sql, params=()):
        conn = db.connect()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def test_candidate_catalog_does_not_materialize_navigation(self):
        self._add_source(path='/tmp/catalog.md', catalog_status='candidate')
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO kp(course_id,name,chapter) VALUES(?,?,?)",
                    (self.course_id, '需求', '第1章'))
        finally:
            conn.close()
        with patch('app.memory.sources.extract_structured',
                   side_effect=AssertionError('candidate catalog must not be parsed for navigation')):
            self.assertEqual(repo.ensure_chapter_catalog('测试课程'), [])
            summary = repo.sync_memory_views('测试课程', materialize_catalog=True)
        self.assertEqual(summary['catalog_rows_removed'], 0)
        self.assertEqual(self._count('SELECT COUNT(*) FROM chapter_catalog'), 0)
        graph = repo.graph_of('测试课程')
        self.assertEqual(graph['chapters'], [])
        self.assertEqual(graph['catalog_status'], 'missing')

    def test_active_catalog_materializes_navigation_and_graph_ready(self):
        source_id = self._add_source(path='/tmp/catalog.md', catalog_status='candidate')
        with patch('app.memory.sources.extract_structured',
                   return_value=('', [], CATALOG_EVIDENCE)):
            result = repo.select_catalog_source('测试课程', source_id)
        self.assertEqual(result['status'], 'active')
        self.assertEqual([chapter['key'] for chapter in result['chapters']], ['ch-01'])
        graph = repo.graph_of('测试课程')
        self.assertEqual([chapter['key'] for chapter in graph['chapters']], ['ch-01'])
        self.assertEqual(graph['catalog_status'], 'ready')
        self.assertEqual(repo.ensure_chapter_catalog('测试课程')[0]['key'], 'ch-01')
        self.assertEqual(self._count(
            "SELECT COUNT(*) FROM source_files WHERE catalog_status='active'"), 1)

    def test_stale_candidate_navigation_rows_are_purged(self):
        source_id = self._add_source(path='/tmp/catalog.md', catalog_status='candidate')
        conn = db.connect()
        try:
            with conn:
                chapter_id = conn.execute(
                    """INSERT INTO chapter_catalog
                           (course_id,source_id,chapter_key,number,title,status)
                       VALUES(?,?,?,?,?,'active')""",
                    (self.course_id, source_id, 'ch-01', 1, '第一章')).lastrowid
                conn.execute(
                    """INSERT INTO chapter_sections(chapter_id,section_key,number,title)
                       VALUES(?,?,?,?)""", (chapter_id, 'ch-01-sec-01', '1.1', '第一节'))
        finally:
            conn.close()
        self.assertEqual(repo.graph_of('测试课程')['catalog_status'], 'missing')
        summary = repo.sync_memory_views('测试课程')
        self.assertEqual(summary['catalog_rows_removed'], 1)
        self.assertEqual(self._count('SELECT COUNT(*) FROM chapter_catalog'), 0)
        self.assertEqual(self._count('SELECT COUNT(*) FROM chapter_sections'), 0)
        graph = repo.graph_of('测试课程')
        self.assertEqual(graph['chapters'], [])
        self.assertEqual(graph['catalog_status'], 'missing')

    def test_topic_clusters_still_navigate_without_any_catalog(self):
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO kp(course_id,name,chapter) VALUES(?,?,?)",
                             (self.course_id, '节点甲', '第一章'))
                conn.execute("INSERT INTO kp(course_id,name,chapter) VALUES(?,?,?)",
                             (self.course_id, '节点乙', '第二章'))
        finally:
            conn.close()
        graph = repo.graph_of('测试课程')
        self.assertEqual([chapter['title'] for chapter in graph['chapters']],
                         ['第一章', '第二章'])
        self.assertEqual(graph['catalog_status'], 'ready')


class IngestRunConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for key, value in {'APP_DIR': self.root / 'app',
                           'DB_PATH': self.root / 'app' / 'test.db'}.items():
            self.stack.enter_context(patch.object(db, key, value))
        for key, value in {'_stop': Event(), '_wake': Event(), '_thread': None,
                           '_lease': None, '_accepting': True}.items():
            self.stack.enter_context(patch.object(jobs, key, value))
        db.apply_schema()
        conn = db.connect()
        try:
            with conn:
                conn.execute("INSERT INTO courses(name) VALUES('ingest-test')")
                self.course_id = conn.execute(
                    'SELECT id FROM courses WHERE name=?', ('ingest-test',)).fetchone()[0]
        finally:
            conn.close()
        self.folder = self.root / 'StudyPal' / 'ingest-test' / 'inbox'
        self.folder.mkdir(parents=True)
        self.addCleanup(jobs.stop)

    def _enqueue(self, name='notes.md'):
        path = self.folder / name
        path.write_text('# Cost\nMarginal cost.\n')
        return jobs.enqueue('ingest-test', path, self.folder)

    def _rows(self, sql, params=()):
        conn = db.connect()
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def test_startup_interrupts_ownerless_jobs_and_runs(self):
        task = self._enqueue()
        jobs._update(task['id'], status='running')
        conn = db.connect()
        try:
            with conn:
                orphan_run = conn.execute(
                    """INSERT INTO ingest_runs(course_id,source_kind,source_role,status)
                       VALUES(?,?,?,'running')""",
                    (self.course_id, 'material', 'material')).lastrowid
        finally:
            conn.close()
        jobs.start()
        try:
            self.assertEqual(jobs.get(task['id'])['status'], 'interrupted')
            job_run = self._rows(
                'SELECT status,finished_at FROM ingest_runs WHERE id=?',
                (task['ingest_run_id'],))[0]
            self.assertEqual(job_run['status'], 'interrupted')
            self.assertTrue(job_run['finished_at'])
            orphan = self._rows(
                'SELECT status,finished_at,error FROM ingest_runs WHERE id=?',
                (orphan_run,))[0]
            self.assertEqual(orphan['status'], 'interrupted')
            self.assertTrue(orphan['finished_at'])
            self.assertIn('识别', orphan['error'])
            self.assertEqual(self._rows(
                "SELECT COUNT(*) AS n FROM ingest_jobs WHERE status='running'")[0]['n'], 0)
            self.assertEqual(self._rows(
                "SELECT COUNT(*) AS n FROM ingest_runs WHERE status='running'")[0]['n'], 0)
        finally:
            jobs.stop()

    def test_worker_heartbeat_keeps_running_job_alive(self):
        task = self._enqueue()
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE ingest_jobs SET updated_at='2000-01-01 00:00:00' WHERE id=?",
                    (task['id'],))
        finally:
            conn.close()
        entered, release = Event(), Event()
        real_extract = sources.extract_structured
        touches = []
        real_touch = jobs._touch_heartbeat

        def touch(task_id):
            touches.append(task_id)
            real_touch(task_id)

        def slow_extract(path):
            entered.set()
            self.assertTrue(release.wait(5))
            return real_extract(path)

        with patch.object(jobs, 'HEARTBEAT_INTERVAL_SECONDS', .05), \
                patch.object(jobs, '_touch_heartbeat', touch), \
                patch.object(sources, 'extract_structured', side_effect=slow_extract):
            thread = Thread(target=jobs.run_one, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and len(touches) < 2:
                    time.sleep(.02)
                self.assertGreaterEqual(len(touches), 2,
                                        'worker did not heartbeat while extraction ran')
                row = self._rows(
                    'SELECT status,updated_at FROM ingest_jobs WHERE id=?',
                    (task['id'],))[0]
                self.assertEqual(row['status'], 'running')
                self.assertNotEqual(row['updated_at'], '2000-01-01 00:00:00',
                                    'heartbeat did not refresh updated_at')
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(jobs.get(task['id'])['status'], 'waiting_review')

    def test_stale_heartbeat_is_reconciled_to_interrupted(self):
        task = self._enqueue()
        jobs._update(task['id'], status='running')
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE ingest_jobs SET updated_at=datetime('now','localtime','-1 hour') "
                    'WHERE id=?', (task['id'],))
                conn.execute(
                    "UPDATE ingest_runs SET status='running',finished_at='' WHERE id=?",
                    (task['ingest_run_id'],))
        finally:
            conn.close()
        self.assertEqual(jobs.reconcile_stale_runs(), 1)
        self.assertEqual(jobs.get(task['id'])['status'], 'interrupted')
        row = self._rows('SELECT status,finished_at FROM ingest_runs WHERE id=?',
                         (task['ingest_run_id'],))[0]
        self.assertEqual(row['status'], 'interrupted')
        self.assertTrue(row['finished_at'])
        self.assertNotIn('running',
                         [job['status'] for job in jobs.list_jobs()])
        self.assertEqual(self._rows(
            "SELECT COUNT(*) AS n FROM ingest_runs WHERE status='running'")[0]['n'], 0)


if __name__ == '__main__':
    unittest.main()
