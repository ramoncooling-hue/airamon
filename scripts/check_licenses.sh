#!/usr/bin/env bash
# T1.14 依赖许可门禁。用法：scripts/check_licenses.sh [--write]
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
cd "$ROOT"
exec "$PY" "$ROOT/scripts/check_licenses.py" "$@"
