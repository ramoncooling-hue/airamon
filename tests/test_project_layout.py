import tempfile
import unittest
from pathlib import Path

from scripts.project_layout_dry_run import scan


class ProjectLayoutDryRunTests(unittest.TestCase):
    def test_scan_is_read_only_and_protects_runtime_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'data' / 'StudyPal' / '会计学').mkdir(parents=True)
            (root / 'data' / 'StudyPal' / '会计学' / '教材.pdf').write_bytes(b'user material')
            (root / 'app').mkdir()
            (root / '.DS_Store').write_bytes(b'generated')
            report = scan(root)
            by_path = {row['path']: row for row in report['entries']}
            self.assertTrue(report['read_only'])
            self.assertEqual(by_path['data']['status'], 'protected')
            self.assertEqual(by_path['data/StudyPal/会计学/教材.pdf']['status'], 'protected')
            self.assertEqual(by_path['.DS_Store']['status'], 'generated')
            self.assertTrue((root / 'data' / 'StudyPal' / '会计学' / '教材.pdf').is_file())


if __name__ == '__main__':
    unittest.main()
