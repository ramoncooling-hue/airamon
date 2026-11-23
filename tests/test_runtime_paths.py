import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import runtime


class RuntimePathTests(unittest.TestCase):
    def test_default_data_dir_is_project_local(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runtime.data_dir(), runtime.DEFAULT_DATA_DIR)

    def test_environment_selects_external_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {'CYBERPAL_DATA_DIR': tmp}):
                self.assertEqual(runtime.data_dir(), Path(tmp))


if __name__ == '__main__':
    unittest.main()
