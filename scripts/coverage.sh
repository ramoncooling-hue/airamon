#!/usr/bin/env bash
# 覆盖率度量（T1.14）。先只出数不设卡；报告写 output/coverage/<stamp>/。
# 用法：scripts/coverage.sh [pytest 追加参数...]
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$ROOT/output/coverage/$STAMP"
mkdir -p "$OUT"
export CYBERPAL_DATA_DIR="$(mktemp -d /tmp/mindock-coverage-XXXXXX)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
"$PY" -m pytest -q --cov=app --cov-report=term-missing \
  --cov-report="xml:$OUT/coverage.xml" --cov-report="html:$OUT/html" "$@" | tee "$OUT/coverage.log"
status=${PIPESTATUS[0]}
echo
echo "覆盖率报告：$OUT"
exit "$status"
