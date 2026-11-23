'''CyberPal runtime paths.

The source tree is portable; mutable user data is selected once at process start.
Development keeps the historical local data/ default. Packaged macOS runs
set CYBERPAL_DATA_DIR to Application Support without changing the code tree.
'''

from __future__ import annotations

import os
import sys
from pathlib import Path

if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    ROOT = Path(sys._MEIPASS)
else:
    ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / 'data'


def data_dir() -> Path:
    configured = os.environ.get('CYBERPAL_DATA_DIR', '').strip()
    return Path(configured).expanduser() if configured else DEFAULT_DATA_DIR
