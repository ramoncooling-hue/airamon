#!/usr/bin/env python3
"""Seed the local WorkPal starter graph from imported Wiki evidence."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.memory.workpal_graph import seed


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False, indent=2))

