import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import db
from scripts.migrate_runtime import migrate
from app.maintenance import files


def small_runtime(path, name):
    (path / 'app').mkdir(parents=True)
    conn = sqlite3.connect(path / 'app/banxue.db')
    conn.execute('CREATE TABLE records(value TEXT)')
    conn.execute('INSERT INTO records VALUES(?)', (name,))
    conn.commit()
    conn.close()
    (path / 'material.txt').write_text(name)
    return path


class RuntimeMigrationTests(unittest.TestCase):
    def test_windows_publication_uses_exclusive_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'stage'
            target = root / 'target'
            source.mkdir()
            with patch.object(files.sys, 'platform', 'win32'), \
                    patch.object(files.ctypes, 'CDLL', side_effect=AssertionError('POSIX branch')):
                files.rename_new(source, target)
            self.assertTrue(target.is_dir())
            self.assertFalse(source.exists())

            source.mkdir()
            with patch.object(files.sys, 'platform', 'win32'), \
                    patch.object(files.os, 'rename', side_effect=FileExistsError('occupied')):
                with self.assertRaises(FileExistsError):
                    files.rename_new(source, target)
            self.assertTrue(source.is_dir())
            self.assertTrue(target.is_dir())

    def test_merge_rejected_without_changing_target_database_or_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = small_runtime(root / 'source', 'source record')
            target = small_runtime(root / 'target', 'personal target record')
            before = {p: p.read_bytes() for p in target.rglob('*') if p.is_file()}
            for merge in (True, False):
                with self.assertRaises(ValueError):
                    migrate(source, target, merge=merge)
                self.assertEqual(before, {p: p.read_bytes() for p in target.rglob('*') if p.is_file()})

    def test_copy_failure_permission_and_disk_full_leave_target_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = small_runtime(root / 'source', 'original')
            for failure in (OSError('copy failed'), PermissionError('denied')):
                target = root / 'target'
                target.mkdir(exist_ok=True)
                with patch.object(files.shutil, 'copy2', side_effect=failure):
                    with self.assertRaises(OSError):
                        migrate(source, target)
                self.assertEqual(list(target.iterdir()), [])
                self.assertEqual((source / 'material.txt').read_text(), 'original')
                self.assertEqual(list(root.glob('.maintenance-*')), [])
            with patch.object(files, 'require_space', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    migrate(source, root / 'new')
            self.assertFalse((root / 'new').exists())

    def test_source_target_containment_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = small_runtime(root / 'source', 'original')
            for target in (source, source / 'nested', root):
                with self.assertRaises(ValueError):
                    migrate(source, target)
            (source / 'escape').symlink_to(root)
            with self.assertRaises(ValueError):
                migrate(source, root / 'target')
            alias = root / 'alias'
            alias.symlink_to(source)
            with self.assertRaises(ValueError):
                migrate(alias, root / 'target')

    def test_target_occupied_during_publication_not_overwritten(self):
        from scripts import migrate_runtime
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = small_runtime(root / 'source', 'original')
            target = root / 'target'
            original = migrate_runtime.rename_new
            def racing_publish(stage, destination):
                destination.mkdir()
                (destination / 'personal').write_text('keep')
                original(stage, destination)
            with patch.object(migrate_runtime, 'rename_new', side_effect=racing_publish):
                with self.assertRaises(FileExistsError):
                    migrate(source, target)
            self.assertEqual((target / 'personal').read_text(), 'keep')
            self.assertFalse((target / 'app').exists())

    def test_missing_internal_reference_fails_before_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = small_runtime(root / 'source', 'original')
            (source / 'app/settings.json').write_text(json.dumps({'path': str(source / 'missing.pdf')}))
            with self.assertRaisesRegex(ValueError, 'missing'):
                migrate(source, root / 'target')
            self.assertFalse((root / 'target').exists())

    def test_migrate_copies_database_settings_and_materials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            target = root / 'target'
            (source / 'app').mkdir(parents=True)
            (source / 'StudyPal' / '会计学').mkdir(parents=True)
            (source / 'app' / 'settings.json').write_text(json.dumps({'fast': {'model': 'local'}}), encoding='utf-8')
            (source / 'StudyPal' / '会计学' / 'notes.md').write_text('材料', encoding='utf-8')
            (source / 'lifepal' / 'notes').mkdir(parents=True)
            (source / 'lifepal' / 'notes' / 'record.md').write_text('生活记录', encoding='utf-8')
            with patch.object(db, 'APP_DIR', source / 'app'), patch.object(db, 'DB_PATH', source / 'app' / 'banxue.db'):
                db.apply_schema()
                conn = db.connect()
                conn.execute("INSERT INTO courses(name) VALUES('会计学')")
                conn.commit()
                conn.close()
            result = migrate(source, target)
            self.assertEqual(result['target'], str(target.resolve()))
            self.assertTrue((target / 'app' / 'banxue.db').exists())
            self.assertTrue((target / 'StudyPal' / '会计学' / 'notes.md').exists())
            self.assertTrue((target / 'lifepal' / 'notes' / 'record.md').exists())
            self.assertEqual(json.loads((target / 'app' / 'settings.json').read_text())['fast']['model'], 'local')


if __name__ == '__main__':
    unittest.main()
