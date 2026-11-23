#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${1:-$ROOT/output/share-mcp-$STAMP}"
OUT_DIR="$(mkdir -p "$OUT_DIR" && cd "$OUT_DIR" && pwd)"
ARCHIVE="$OUT_DIR/CyberPal-source-v${VERSION}-mcp-candidate-$STAMP.zip"
TREE_DIR="${2:-$OUT_DIR/source}"
if [[ "$TREE_DIR" != /* ]]; then
  TREE_DIR="$ROOT/$TREE_DIR"
fi
if [[ -e "$TREE_DIR" || -L "$TREE_DIR" ]]; then
  echo "Refusing to overwrite existing share tree: $TREE_DIR" >&2
  exit 2
fi
if [[ -e "$ARCHIVE" || -L "$ARCHIVE" ]]; then
  echo "Refusing to overwrite existing share archive: $ARCHIVE" >&2
  exit 2
fi
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
python3 "$ROOT/scripts/zip_share_utf8.py" --stage "$ROOT" "$STAGE"
# Keep the public repository focused on runnable code and technical docs.
find "$STAGE/docs" -type f \( \
  -name 'BRAND-INTRO-DRAFT-*.md' -o \
  -name 'MinDock-*' -o \
  -name 'DEMO-*' -o \
  -name 'CLEANUP-*.md' -o \
  -name 'DELIVERY-REVIEW.md' -o \
  -name 'QUICK-REVIEW-*.md' -o \
  -name 'SHARE-READINESS-REVIEW-*.md' -o \
  -name 'PROJECT-LAYOUT-DRY-RUN-*.md' -o \
  -path '*/superpowers/*' -o \
  -path '*/brand-manual-assets/*' -o \
  -path '*/platform/平台定义与使用手册.md' \
\) -delete
cp "$ROOT/README.md" "$ROOT/LICENSE" "$ROOT/VERSION" "$ROOT/CHANGELOG.md" "$ROOT/requirements.txt" "$ROOT/.gitignore" "$ROOT/启动伴学.command" "$ROOT/start_cyberpal.bat" "$ROOT/DEMO-页面索引.md" "$STAGE/"
cp "$ROOT/requirements-extensions.txt" "$STAGE/"
cp "$ROOT/requirements-retrieval-eval.txt" "$STAGE/"
rm -f "$STAGE/DEMO-页面索引.md"
mkdir -p "$STAGE/seed"
cp "$ROOT"/seed/public/* "$STAGE/seed/"
cp -R "$ROOT/seed/public" "$STAGE/seed/public"
cd "$STAGE"
python3 "$ROOT/scripts/zip_share_utf8.py" "$STAGE" "$ARCHIVE"

if [[ -e "$TREE_DIR" ]]; then
  echo "Refusing to overwrite existing share tree: $TREE_DIR" >&2
  exit 2
fi
mkdir -p "$(dirname "$TREE_DIR")"
cp -R "$STAGE" "$TREE_DIR"

echo "$ARCHIVE"
echo "$TREE_DIR"
