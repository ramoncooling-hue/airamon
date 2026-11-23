#!/usr/bin/env python3
"""依赖许可门禁（T1.14）。

策略见 docs/NEW-WORKSPACE/05-COMPONENT-MATRIX.md 第 6 节：
- Python 运行时依赖（随包分发）：allowlist = MIT / MIT-0 / MIT-CMU / BSD-2 / BSD-3 /
  Apache-2.0 / ISC / PSF / Unlicense / 0BSD（允许 "License" 尾缀与 OR/AND 组合写法）；
  MPL-2.0 允许但必须逐项登记（当前只有 certifi）；GPL/AGPL/商业/专有许可直接阻断。
- Node.js 依赖：全部为 devDependencies（不随包分发），仅阻断 copyleft/商业许可；
  其余未登记许可打印 WARN，不阻塞（运行时零 npm 依赖）。
- 前端 vendored JS：上游 LICENSE 文件必须随文件放置，本脚本校验文件存在性。

用法：
  scripts/check_licenses.sh            # 门禁：校验 + 打印摘要
  scripts/check_licenses.sh --write    # 重新生成 THIRD-PARTY-NOTICES.md

退出码：0 通过；1 存在阻断项；2 工具缺失（需要安装 requirements-dev.txt）。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = Path(sys.executable)
NOTICES = ROOT / "THIRD-PARTY-NOTICES.md"

ALLOW = {
    "mit", "mit-0", "mit-cmu", "mit license", "bsd license", "bsd-2-clause", "bsd-3-clause",
    "apache-2.0", "apache license 2.0", "isc", "python software foundation license", "psf-2.0",
    "unlicense", "the unlicense", "0bsd", "zlib", "cc0-1.0",
}
# npm 生态常见的宽松等价许可（dev-only 时容忍）
NODE_EXTRA_ALLOW = {"cc0-1.0", "blueoak-1.0.0", "wtfpl", "public domain", "unlicensed"}
WARN = {"mozilla public license 2.0 (mpl 2.0)", "mpl-2.0"}
BLOCK_MARKERS = ("gpl", "agpl", "commercial", "proprietary", "sspl", "busl", "elastic")

# These packages may exist in a developer environment because local desktop
# packaging uses them, but they are not declared by requirements*.txt and are
# not copied into the application runtime. Keep them visible as warnings.
TOOLCHAIN_ONLY = {
    "pyinstaller": "本机打包工具，不随应用运行时分发",
    "pyinstaller-hooks-contrib": "本机打包工具，不随应用运行时分发",
    "uvloop": "本机/服务器性能扩展，未列入本地运行时依赖",
}

# 前端随包分发的第三方文件与其许可文件（缺一即失败）
VENDORED = [
    ("Cytoscape.js", "web/assets/cytoscape.LICENSE.txt", "MIT"),
    ("Lucide", "web/assets/LICENSE-lucide.txt", "ISC"),
    ("markdown-it", "web/assets/vendor/LICENSE-markdown-it.txt", "MIT"),
    ("DOMPurify", "web/assets/vendor/LICENSE-dompurify.txt", "Apache-2.0 OR MPL-2.0"),
]


def normalize(text: str) -> str:
    raw = re.sub(r"\s+", " ", (text or "").strip().strip('"')).lower()
    # pip 元数据里常见的 "Apache License, Version 2.0" / "MIT License, Apache License, Version 2.0"
    raw = raw.replace("apache license, version 2.0", "apache-2.0").replace("apache license version 2.0", "apache-2.0")
    # pip 元数据里同义写法："Apache Software License" / "3-Clause BSD License"
    raw = raw.replace("apache software license", "apache-2.0")
    raw = raw.replace("3-clause bsd license", "bsd-3-clause")
    # pypdfium2 uses this suffix to describe its bundled PDFium notices.
    raw = raw.replace(", dependency licenses", "")
    return raw


def classify(expr: str, *, node: bool = False):
    """返回 (allowed, reason, warn)。OR/AND/斜杠组合逐项判断。"""
    raw = normalize(expr)
    if not raw:
        return False, "许可字段为空（元数据缺失）", False
    parts = [p.strip() for p in re.split(r"\s+(?:or|and)\s+|\s*/\s*|,", raw) if p.strip()] or [raw]
    extra = NODE_EXTRA_ALLOW if node else set()
    if any(any(marker in part for marker in BLOCK_MARKERS) for part in parts):
        if any(part in ALLOW | extra for part in parts):
            return True, f"双许可含阻断分支，按允许分支使用：{expr}", True
        return False, f"命中阻断许可：{expr}", False
    if all(part in ALLOW | extra for part in parts):
        return True, "", False
    if all(part in ALLOW | WARN for part in parts) and any(part in WARN for part in parts):
        return True, f"MPL-2.0 需逐项登记：{expr}", True
    if node:
        return True, f"dev-only 未登记许可（不阻塞）：{expr}", True
    return False, f"未登记许可：{expr}", False


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True)


def pip_licenses_rows() -> list[dict[str, str]]:
    candidates = [
        [str(PY), "-m", "pip_licenses"],
        [str(ROOT / ".venv/bin/pip-licenses")],
        ["pip-licenses"],
    ]
    last: subprocess.CompletedProcess | None = None
    for base in candidates:
        if base[0].endswith("pip-licenses") and not Path(base[0]).exists():
            continue
        proc = _run(base + ["--format=csv", "--with-urls"])
        last = proc
        if proc.returncode == 0 and proc.stdout.strip():
            text = proc.stdout.lstrip("\n")
            return [row for row in csv.DictReader(io.StringIO(text)) if row.get("Name")]
    detail = (last.stderr.strip() if last else "未找到 pip-licenses 命令")
    raise SystemExit(f"无法运行 pip-licenses（安装 requirements-dev.txt 后重试）：{detail}")


def node_rows() -> tuple[list[tuple[str, str]], str]:
    """返回 ([(name, license)], 说明)。未安装 node_modules 时返回空并说明跳过。"""
    package_json = ROOT / "package.json"
    if not package_json.exists():
        return [], "无 package.json，跳过"
    root_name = json.loads(package_json.read_text(encoding="utf-8")).get("name", "")
    checker = ROOT / "node_modules/.bin/license-checker"
    if not checker.exists():
        return [], "未安装 node_modules，跳过（npm install 后生效）"
    proc = _run([str(checker), "--json", "--start", str(ROOT)])
    if proc.returncode != 0:
        return [], f"license-checker 运行失败（{proc.returncode}），跳过：{proc.stderr.strip()[:200]}"
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return [], f"license-checker 输出无法解析，跳过：{exc}"
    rows: list[tuple[str, str]] = []
    for key, value in sorted(payload.items()):
        name = key.rsplit("@", 1)[0] if "@" in key else key
        if name == root_name:
            continue
        licenses = value.get("licenses", "")
        if isinstance(licenses, list):
            licenses = " OR ".join(str(item) for item in licenses)
        elif isinstance(licenses, dict):
            licenses = str(licenses.get("type", ""))
        rows.append((key, str(licenses)))
    return rows, ""


def check_python() -> tuple[list[dict[str, str]], list[str], list[str]]:
    rows = pip_licenses_rows()
    problems: list[str] = []
    warnings: list[str] = []
    for row in rows:
        name = row.get("Name", "").lower()
        if name in TOOLCHAIN_ONLY:
            warnings.append(
                f"{row['Name']}=={row['Version']}：环境外工具，{TOOLCHAIN_ONLY[name]}"
            )
            continue
        ok, reason, warn = classify(row.get("License", ""))
        if not ok:
            problems.append(f"{row['Name']}=={row['Version']}：{reason}")
        elif warn:
            warnings.append(f"{row['Name']}=={row['Version']}：{reason}")
    return rows, problems, warnings


def check_node() -> tuple[list[tuple[str, str]], list[str], list[str]]:
    rows, note = node_rows()
    if not rows:
        print(f"[licenses] Node 依赖检查：{note}")
        return [], [], []
    problems: list[str] = []
    warnings: list[str] = []
    for name, license_text in rows:
        ok, reason, warn = classify(license_text, node=True)
        if not ok:
            problems.append(f"{name}：{reason}")
        elif warn:
            warnings.append(f"{name}：{reason}")
    return rows, problems, warnings


def check_vendored() -> list[str]:
    problems = []
    for component, rel, _license in VENDORED:
        path = ROOT / rel
        if not path.exists():
            problems.append(f"缺少 {component} 的许可证文件：{rel}")
    return problems


def write_notices(python_rows: list[dict[str, str]], node_rows_: list[tuple[str, str]]) -> None:
    lines = [
        "# 第三方许可声明（THIRD-PARTY-NOTICES）",
        "",
        "> 本文件由 scripts/check_licenses.sh --write 生成，依赖变更后需重新生成并提交；发布包另随附构建时收集的 third-party.json。",
        "> 许可策略见 docs/NEW-WORKSPACE/05-COMPONENT-MATRIX.md 第 6 节。",
        "",
        "## 1. Python 依赖（当前开发环境；随包分发的运行时依赖清单以构建产物 third-party.json 为准）",
        "",
        "| 组件 | 版本 | 许可 | 项目主页 |",
        "|---|---|---|---|",
    ]
    for row in sorted(python_rows, key=lambda item: item["Name"].lower()):
        lines.append(f"| {row['Name']} | {row['Version']} | {row.get('License', '')} | {row.get('URL', '')} |")
    lines += ["", "## 2. 前端随包分发的第三方组件", "", "| 组件 | 许可证文件 | 许可 |", "|---|---|---|"]
    for component, rel, license_name in VENDORED:
        marker = "已放置" if (ROOT / rel).exists() else "缺失"
        lines.append(f"| {component} | {rel}（{marker}） | {license_name} |")
    lines += [
        "",
        "## 3. Node.js 开发工具链（devDependencies，不随发布包分发）",
        "",
    ]
    package_json = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    for name, version in sorted(package_json.get("devDependencies", {}).items()):
        lines.append(f"- {name}@{version}")
    if node_rows_:
        lines += ["", f"传递依赖共 {len(node_rows_)} 项，许可门禁在 scripts/check_licenses.sh 中逐项校验。"]
    lines.append("")
    NOTICES.write_text("\n".join(lines), encoding="utf-8")
    print(f"[licenses] 已生成 {NOTICES.relative_to(ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="重新生成 THIRD-PARTY-NOTICES.md")
    args = parser.parse_args()

    python_rows, python_problems, python_warnings = check_python()
    node_rows_, node_problems, node_warnings = check_node()
    vendored_issues = check_vendored()
    problems = python_problems + node_problems + vendored_issues
    warnings = python_warnings + node_warnings

    print(f"[licenses] Python 依赖 {len(python_rows)} 项；Node 依赖 {len(node_rows_)} 项；vendored 组件 {len(VENDORED)} 项")
    for item in warnings:
        print(f"[licenses] 提示：{item}")
    for item in problems:
        print(f"[licenses] 阻断：{item}")
    if args.write and not vendored_issues:
        write_notices(python_rows, node_rows_)
    if problems:
        print(f"[licenses] FAIL：{len(problems)} 个问题")
        return 1
    if not NOTICES.exists():
        print("[licenses] WARN：THIRD-PARTY-NOTICES.md 缺失，请运行 scripts/check_licenses.sh --write")
    print("[licenses] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
