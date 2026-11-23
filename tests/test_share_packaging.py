import subprocess
import tempfile
import unittest
import shutil
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]


class SharePackagingTests(unittest.TestCase):
    def test_share_builder_includes_desktop_and_refuses_existing_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'scripts').mkdir()
            for script in ('build_share_zip.sh', 'zip_share_utf8.py'):
                shutil.copy2(ROOT / 'scripts' / script, root / 'scripts' / script)
            for folder in ('app', 'web', 'skills', 'agents', 'tests', 'docs', 'domains', 'desktop/windows', 'seed/public'):
                (root / folder).mkdir(parents=True, exist_ok=True)
            for name in ('README.md', 'LICENSE', 'CHANGELOG.md', 'requirements.txt', '.gitignore',
                         '启动伴学.command', 'start_cyberpal.bat', 'DEMO-页面索引.md',
                         'requirements-extensions.txt', 'requirements-retrieval-eval.txt',
                         'desktop/backend.spec', 'desktop/windows/package-lock.json',
                         'desktop/windows/package.json', 'seed/public/manifest.json'):
                (root / name).write_text('public fixture')
            (root / 'VERSION').write_text('0.5.4')
            for name in ('desktop/windows/node_modules/pkg/index.js', 'desktop/output/app.bin',
                         'desktop/settings.json', 'desktop/unlisted.bin', 'app/__pycache__/test.pyc'):
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('must not ship')
            out = root / 'share-mcp-20261014'
            tree = root / 'share-source-mcp-20261014'
            result = subprocess.run(
                ['bash', str(root / 'scripts/build_share_zip.sh'), str(out), str(tree)],
                cwd=root, capture_output=True, text=True, check=True,
            )
            archive = Path(result.stdout.splitlines()[0])
            self.assertTrue(archive.is_file())
            self.assertTrue((tree / 'desktop/backend.spec').is_file())
            self.assertTrue((tree / 'desktop/windows/package-lock.json').is_file())
            with ZipFile(archive) as package:
                names = set(package.namelist())
            self.assertIn('desktop/backend.spec', names)
            self.assertIn('desktop/windows/package.json', names)
            self.assertIn('seed/public/manifest.json', names)
            self.assertIn('seed/manifest.json', names)
            self.assertNotIn('desktop/settings.json', names)
            self.assertNotIn('desktop/unlisted.bin', names)
            self.assertNotIn('desktop/windows/node_modules/', ''.join(names))
            self.assertFalse(any('/output/' in name or name.startswith('output/') for name in names))
            second = subprocess.run(
                ['bash', str(root / 'scripts/build_share_zip.sh'), str(out), str(tree)],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(second.returncode, 2)
            self.assertIn('Refusing to overwrite existing share tree', second.stderr)
            original = archive.read_bytes()
            overwrite = subprocess.run(['python3', str(root / 'scripts/zip_share_utf8.py'), str(tree), str(archive)],
                                       capture_output=True, text=True)
            self.assertNotEqual(overwrite.returncode, 0)
            self.assertEqual(original, archive.read_bytes())


if __name__ == '__main__':
    unittest.main()
