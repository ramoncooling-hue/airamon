import tempfile
import unittest
from pathlib import Path

from scripts.verify_local_install import verify


class LocalInstallVerificationTests(unittest.TestCase):
    def test_isolated_first_run_creates_schema_without_runtime_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = verify(Path(tmp) / 'runtime')
            self.assertTrue(result['ok'])
            self.assertEqual(result['missing'], [])
            self.assertEqual(result['invalid_manifests'], [])
            self.assertEqual(result['secret_hits'], [])
            self.assertTrue((Path(tmp) / 'runtime' / 'app' / 'banxue.db').exists())
            self.assertEqual(result['tables']['courses'], 0)


if __name__ == '__main__':
    unittest.main()
