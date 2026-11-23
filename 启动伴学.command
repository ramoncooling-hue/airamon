#!/bin/bash
# CyberPal / StudyPal · 双击启动。已在运行则只打开浏览器。
cd "$(dirname "$0")" || exit 1

# Source bundles keep data beside the checkout by default. Installers or
# advanced users can set CYBERPAL_DATA_DIR to another local data directory.
export CYBERPAL_DATA_DIR="${CYBERPAL_DATA_DIR:-$PWD/data}"

PORT=8899
URL="http://127.0.0.1:$PORT/"

if curl -sf -m 2 "$URL""api/health" >/dev/null 2>&1; then
    echo "CyberPal / StudyPal 已在运行，打开浏览器…"
    open "$URL"
    exit 0
fi

if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
    echo "错误：端口 $PORT 被其他程序占用。CyberPal / StudyPal 固定用 8899，请先释放再重试。"
    exit 1
fi

PY="${CYBERPAL_PYTHON:-$(command -v python3)}"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    echo "错误：找不到 python3。请先安装 Python 3.11 或更高版本，再重新运行。"
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "首次运行：创建虚拟环境并安装依赖…"
    "$PY" -m venv .venv || { echo "错误：虚拟环境创建失败"; exit 1; }
    ./.venv/bin/python -m pip install --upgrade pip -q
    PIP_ARGS=()
    if [ -n "${CYBERPAL_PYPI_INDEX:-}" ]; then
        PIP_ARGS+=("-i" "$CYBERPAL_PYPI_INDEX")
    fi
    ./.venv/bin/python -m pip install -r requirements.txt "${PIP_ARGS[@]}" || {
        echo "错误：依赖安装失败，请检查网络后重新运行"
        exit 1
    }
fi

echo "启动 CyberPal / StudyPal… $URL"
( sleep 2; open "$URL" ) >/dev/null 2>&1 &
exec ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
