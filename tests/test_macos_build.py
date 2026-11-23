import tempfile
import os
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_macos


class MacBuildTests(unittest.TestCase):
    def test_default_privacy_gate_does_not_discover_personal_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / 'public.txt').write_text('public fixture')
            with patch.object(Path, 'home', side_effect=AssertionError('must not discover user data')), \
                 patch.object(Path, 'read_text', side_effect=AssertionError('must not read settings')):
                build_macos.verify_private_files(bundle)

    def test_privacy_gate_scans_windows_and_custom_runtime_credentials(self):
        for variable, suffix in (('LOCALAPPDATA', 'MinDock/app/settings.json'),
                                 ('CYBERPAL_DATA_DIR', 'app/settings.json')):
            with self.subTest(variable=variable), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                data = root / 'external'
                settings = data / suffix
                settings.parent.mkdir(parents=True)
                secret = 'test-only-external-key-123456789'
                settings.write_text(json.dumps({'model': {'api_key': secret}}))
                bundle = root / 'bundle'
                bundle.mkdir()
                (bundle / 'ordinary.txt').write_text(secret)
                with patch.dict(os.environ, {variable: str(data)}, clear=True), \
                     patch.object(build_macos, 'ROOT', root), patch.object(Path, 'home', return_value=root):
                    with self.assertRaises(RuntimeError) as result:
                        build_macos.verify_private_files(bundle, credential_files=(settings,))
                    self.assertNotIn(secret, str(result.exception))

    def test_privacy_gate_detects_known_key_without_printing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = root / 'data/app/settings.json'
            settings.parent.mkdir(parents=True)
            secret = 'fixture-secret-value-123456'
            settings.write_text('{"channels":{"tsinghua_password":"' + secret + '"}}')
            resources = root / 'bundle'
            resources.mkdir()
            # Straddling the scanner's chunk boundary must still be detected.
            (resources / 'fixture.bin').write_bytes(b'x' * (1024 * 1024 - 8) + secret.encode())
            with patch.object(build_macos, 'ROOT', root), patch.object(Path, 'home', return_value=root):
                with self.assertRaises(RuntimeError) as result:
                    build_macos.verify_private_files(resources, credential_files=(settings,))
                self.assertIn('fixture.bin', str(result.exception))
                self.assertNotIn(secret, str(result.exception))

    def test_privacy_gate_rejects_runtime_database_without_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'banxue.db').touch()
            with patch.object(build_macos, 'ROOT', root), patch.object(Path, 'home', return_value=root):
                with self.assertRaisesRegex(RuntimeError, 'banxue.db'):
                    build_macos.verify_private_files(root)

    def test_native_file_discovery_ignores_symlink_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native = root / 'binary'
            native.write_bytes(b'\xcf\xfa\xed\xfe' + b'0' * 32)
            (root / 'alias').symlink_to(native)
            (root / 'text.txt').write_text('plain text')
            self.assertEqual(build_macos.native_files(root), [native])

    def test_source_snapshot_declares_optional_extension_contract(self):
        snapshot = build_macos.source_snapshot()
        self.assertIn('requirements-extensions.txt', snapshot)
        self.assertIn('app/integrations/mcp_bridge.py', snapshot)
        self.assertIn('app/memory/vector_index.py', snapshot)

    def test_privacy_gate_rejects_local_extensions_and_database_sidecars(self):
        for relative in ('app/extensions/mcp.json', 'app/vector-index/studypal.sqlite',
                         'banxue.db-wal', 'banxue.db-shm', '.env.local',
                         'app/extensions/custom-service.json'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
                with patch.object(build_macos, 'ROOT', root), patch.object(Path, 'home', return_value=root):
                    with self.assertRaises(RuntimeError):
                        build_macos.verify_private_files(root)


if __name__ == '__main__':
    unittest.main()
