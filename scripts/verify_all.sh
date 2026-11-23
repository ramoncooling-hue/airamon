#!/usr/bin/env bash
# MinDock 单一验证入口（新工作区）
# 用法：scripts/verify_all.sh
# 前置：已创建 .venv（python3.13）；完整 pytest 门禁需要 requirements-extensions.txt 已安装。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-$ROOT/.venv/bin/python}"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG_DIR="$ROOT/output/verify/$STAMP"
DATA_DIR="$(mktemp -d /tmp/mindock-verify-XXXXXX)"
mkdir -p "$LOG_DIR"

export CYBERPAL_DATA_DIR="$DATA_DIR"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

FAILED=0
run_step() {
  local name="$1"; shift
  local log="$LOG_DIR/${name}.log"
  echo "--- [${name}] $*"
  if "$@" >"$log" 2>&1; then
    echo "    PASS ${name}"
  else
    echo "    FAIL ${name} (log: ${log})"
    FAILED=1
  fi
}

if [ ! -x "$PY" ]; then
  echo "缺少 Python 解释器：${PY}（先创建 .venv）"
  exit 2
fi

# 覆盖率度量默认关闭；COVERAGE=1 时同一次 pytest 生成 coverage.xml（先只出数不设卡）。
PYTEST_ARGS=(-q)
if [ "${COVERAGE:-0}" = "1" ]; then
  PYTEST_ARGS+=("--cov=app" "--cov-report=term-missing" "--cov-report=xml:$LOG_DIR/coverage.xml")
fi
run_step pytest "$PY" -m pytest "${PYTEST_ARGS[@]}"
if command -v node >/dev/null 2>&1; then
  run_step node-test node --test tests/*.test.cjs
else
  echo "--- [node-test] SKIP: 未找到 node（设置 SKIP_NODE=1 可显式忽略）"
  if [ "${SKIP_NODE:-0}" != "1" ]; then FAILED=1; fi
fi
run_step compileall "$PY" -m compileall -q app scripts desktop
run_step verify-local-install "$PY" scripts/verify_local_install.py --data-dir "$DATA_DIR/local-install"
run_step git-diff-check git diff --check
run_step licenses scripts/check_licenses.sh

echo
echo "日志目录：${LOG_DIR}"
echo "数据目录：${DATA_DIR}（测试数据，可删除）"
if [ "$FAILED" -eq 0 ]; then
  echo "全部通过"
else
  echo "存在失败项，见上方日志"
fi
exit "$FAILED"
