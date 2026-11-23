import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.desktop import DesktopAccessGuard, initialize_data_dir
from scripts.migrate_runtime import migrate


class DesktopTests(unittest.TestCase):
    def test_creates_private_fixed_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = initialize_data_dir(Path(tmp) / 'MinDock')
            if os.name == 'posix':
                self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertTrue((root / 'app' / 'logs').is_dir())

    def test_desktop_requires_session_and_same_origin(self):
        app = FastAPI()
        app.add_middleware(DesktopAccessGuard, token='private-session')

        @app.get('/api/health')
        def health():
            return {'status': 'ok'}

        client = TestClient(app, base_url='http://127.0.0.1:6543')
        self.assertEqual(client.get('/api/health').status_code, 403)
        headers = {'X-MinDock-Session': 'private-session'}
        self.assertEqual(client.get('/api/health', headers=headers).status_code, 200)
        self.assertEqual(client.post('/api/desktop/shutdown', headers=headers).status_code, 404)
        self.assertEqual(client.get('/api/health', headers={**headers, 'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(client.get('/api/health', headers={**headers, 'Host': 'evil.example:6543'}).status_code, 403)
        client.cookies.set('mindock_session', 'private-session')
        self.assertEqual(client.get('/api/health').status_code, 200)
        client.close()

    def test_migration_remaps_structured_paths_without_rewriting_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp).resolve() / 'source', Path(tmp).resolve() / 'target'
            (source / 'app').mkdir(parents=True)
            material = source / 'StudyPal' / 'a.pdf'
            material.parent.mkdir()
            material.write_bytes(b'pdf')
            conn = sqlite3.connect(source / 'app' / 'banxue.db')
            conn.execute('CREATE TABLE source_files(id INTEGER PRIMARY KEY,path TEXT,metadata_json TEXT,detail TEXT,source TEXT)')
            conn.execute('INSERT INTO source_files VALUES(1,?,?,?,?)', (
                str(material), json.dumps({'artifact': str(material), 'external': '/tmp/external.pdf'}),
                'Historical note mentioning ' + str(material), '教材：' + str(material) + ':p5'))
            conn.commit()
            conn.close()
            (source / 'app' / 'settings.json').write_text(json.dumps({'local_sources': {'a': [str(source / 'StudyPal'), '/tmp/external']}}))
            migrate(source, target)
            with sqlite3.connect(target / 'app' / 'banxue.db') as conn:
                path, metadata, detail, provenance = conn.execute('SELECT path,metadata_json,detail,source FROM source_files').fetchone()
            self.assertEqual(path, str(target / 'StudyPal' / 'a.pdf'))
            self.assertEqual(json.loads(metadata)['artifact'], path)
            self.assertEqual(json.loads(metadata)['external'], '/tmp/external.pdf')
            self.assertIn(str(material), detail)
            self.assertEqual(provenance, '教材：' + path + ':p5')
            settings = json.loads((target / 'app' / 'settings.json').read_text())
            self.assertEqual(settings['local_sources']['a'][0], str(target / 'StudyPal'))
            self.assertEqual(material.read_bytes(), b'pdf')
            with self.assertRaises(ValueError):
                migrate(source, target)

    def test_desktop_uses_bundled_image_ocr(self):
        from app.memory import sources
        with patch.object(sources, '_native_ocr', return_value='/app/mindock-ocr'), \
                patch.object(sources, '_native_ocr_text', return_value='课程日期变更') as ocr:
            self.assertEqual(sources._tesseract_text(Path('/tmp/image.png')), '课程日期变更')
            ocr.assert_called_once_with(Path('/tmp/image.png'))

    def test_frozen_seed_refresh_does_not_require_python_script(self):
        from app.memory import seed, db
        from scripts import load_memory
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public = root / 'seed'
            public.mkdir()
            (public / 'dash_courses.json').write_text('[]')
            with patch.object(seed, 'APP_DIR', root / 'app'), \
                    patch.object(seed, 'SEED_DIR', public), \
                    patch.object(db, 'APP_DIR', root / 'app'), \
                    patch.object(db, 'DB_PATH', root / 'app/db.sqlite'), \
                    patch('sys.frozen', True, create=True), \
                    patch.object(load_memory, 'DATA', public), \
                    patch.object(load_memory, 'main') as load:
                db.apply_schema()
                self.assertTrue(seed.refresh()['ok'])
                load.assert_called_once_with([])


if __name__ == '__main__':
    unittest.main()
