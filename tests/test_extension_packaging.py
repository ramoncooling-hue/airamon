import tempfile
import unittest
import json
import zipfile
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

from scripts.check_extension_packaging import inspect, verify_extension_payload
from scripts import build_windows


class ExtensionPackagingTests(unittest.TestCase):
    def test_windows_package_version_matches_project_version(self):
        version = (build_windows.ROOT / 'VERSION').read_text().strip()
        package = json.loads((build_windows.ROOT / 'desktop/windows/package.json').read_text())
        lock = json.loads((build_windows.ROOT / 'desktop/windows/package-lock.json').read_text())
        self.assertEqual(package['version'], version)
        self.assertEqual(lock['version'], version)
        self.assertEqual(lock['packages']['']['version'], version)

    def test_windows_build_rejects_version_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'VERSION').write_text('9.9.9')
            (root / 'desktop/windows').mkdir(parents=True)
            (root / 'desktop/windows/package.json').write_text(json.dumps({'version': '9.9.8'}))
            (root / 'desktop/windows/package-lock.json').write_text(json.dumps({
                'version': '9.9.9', 'packages': {'': {'version': '9.9.9'}}}))
            with patch.object(build_windows, 'ROOT', root):
                with self.assertRaisesRegex(RuntimeError, 'package version mismatch'):
                    build_windows.assert_version_consistency()

    def test_declarations_are_distinct_from_native_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'desktop/windows').mkdir(parents=True)
            (root / 'requirements-extensions.txt').write_text('mcp==2.3.0\nsqlite-vec==0.1.9\n')
            (root / 'desktop/requirements-lock.txt').write_text('mcp==2.3.0\nsqlite_vec==0.1.9\n')
            lock = root / 'desktop/windows/requirements-lock.txt'
            lock.write_text('mcp==2.2.0 --hash=sha256:fake\n')
            report = inspect(root)
            self.assertEqual(report['targets']['macos']['dependency_declarations'], 'matched')
            self.assertEqual(report['targets']['macos']['native_runtime_acceptance'], 'not_checked')
            self.assertEqual(report['targets']['windows']['missing'], ['sqlite-vec'])
            self.assertEqual(report['targets']['windows']['mismatched'], ['mcp'])

    def test_payload_gate_checks_native_library_and_transport_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sqlite_vec').mkdir()
            (root / 'mcp/client').mkdir(parents=True)
            (root / 'pywin32_system32').mkdir()
            (root / 'sqlite_vec/vec0.dll').write_bytes(b'MZ' + b'payload')
            for relative in ('mcp/client/stdio.py', 'mcp/client/streamable_http.py',
                             'pywin32.pth', 'win32/lib/pywin32_bootstrap.py',
                             'pywin32_system32/pywintypes312.dll',
                             'pywin32_system32/pythoncom312.dll'):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'payload')
            report = verify_extension_payload(root, 'windows')
            self.assertEqual(report['native_runtime_acceptance'], 'not_checked')

    def test_payload_gate_rejects_missing_native_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, 'vec0.dll'):
                verify_extension_payload(root, 'windows')

    def test_windows_lock_rejects_extra_cache_wheel_and_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'lock.txt'
            lock.write_text('mcp==2.3.0 --hash=sha256:' + 'a' * 64 + '\n')
            valid = {'name': 'mcp', 'version': '2.3.0', 'sha256': 'a' * 64}
            build_windows.verify_wheel_lock([valid], lock)
            with self.assertRaisesRegex(RuntimeError, 'hash'):
                build_windows.verify_wheel_lock([{**valid, 'sha256': 'b' * 64}], lock)
            with self.assertRaisesRegex(RuntimeError, 'exactly match'):
                build_windows.verify_wheel_lock([valid, {'name': 'extra', 'version': '1'}], lock)

    def test_windows_dependency_markers_and_extras_are_checked_for_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def wheel(name, requires=''):
                path = root / (name + '.whl')
                with zipfile.ZipFile(path, 'w') as archive:
                    archive.writestr(name + '-1.dist-info/METADATA',
                                     f'Name: {name}\nVersion: 1\n' + requires)
                return path

            sdk = wheel('mcp', 'Requires-Dist: pywin32>=1; sys_platform == "win32"\n'
                              'Requires-Dist: pyjwt[crypto]>=1\n')
            jwt = wheel('pyjwt', 'Requires-Dist: cryptography>=1; extra == "crypto"\n')
            with self.assertRaisesRegex(RuntimeError, 'pywin32'):
                build_windows.verify_wheel_dependencies([sdk, jwt])
            win = wheel('pywin32')
            with self.assertRaisesRegex(RuntimeError, 'cryptography'):
                build_windows.verify_wheel_dependencies([sdk, jwt, win])
            build_windows.verify_wheel_dependencies([sdk, jwt, win, wheel('cryptography')])

    def test_candidate_config_routes_all_outputs_and_preserves_old_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'candidate'
            evidence = output / '20261014-123456'
            evidence.mkdir(parents=True)
            previous = root / 'MinDock-0.5.4-Windows-x64-Setup.exe'
            previous.write_bytes(b'previous artifact')
            config_path = build_windows.candidate_builder_config(output, evidence, '0.5.4', evidence.name)
            config = json.loads(config_path.read_text())
            self.assertEqual(config['extraResources'][0]['from'], str(output / 'staging/backend'))
            self.assertEqual(config['directories']['output'], str(evidence / 'installer'))
            self.assertIn('mcp-candidate-20261014-123456', config['nsis']['artifactName'])
            self.assertFalse(config['nsis']['differentialPackage'])
            self.assertTrue(config['nsis']['useZip'])
            self.assertNotIn('../../output/windows', (evidence / 'after-pack.cjs').read_text())
            self.assertEqual(previous.read_bytes(), b'previous artifact')

    def test_source_changes_during_windows_preparation_abort_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(build_windows, 'snapshot', side_effect=[{'app.py': 'old'}, {'app.py': 'new'}]), \
                 patch.object(build_windows, 'prepare', return_value=(Path(directory), [])), \
                 patch.object(build_windows, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'changed during preparation'):
                    build_windows.build(Path(directory))
                run.assert_not_called()

    def test_frozen_hook_normal_launch_does_not_execute_cli_or_open_files(self):
        hook = Path(__file__).resolve().parents[1] / 'scripts/check_extension_packaging.py'
        with patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, 'argv', ['mindock-backend']), \
             patch.object(Path, 'read_text', side_effect=AssertionError('must not read configuration')):
            runpy.run_path(str(hook), run_name='__main__')

    def test_preparation_rejects_missing_lock_before_pip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'desktop/windows').mkdir(parents=True)
            (root / 'desktop/windows/runtime-artifacts.json').write_text('{}')
            (root / 'requirements-extensions.txt').write_text('mcp==2.3.0\nsqlite-vec==0.1.9\n')
            with patch.object(build_windows, 'ROOT', root), patch.object(build_windows, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'lock is missing'):
                    build_windows.prepare(root / 'out')
                run.assert_not_called()

