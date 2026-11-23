"""Searchable, bundled MinDock platform documentation.

The platform guide is deliberately kept outside the course knowledge graph.
It explains the product itself, so answers can stay current when course data
is empty or the user has selected a specific course scope.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from ..runtime import ROOT

DOCS_DIR = ROOT / 'docs' / 'platform'
_CACHE_KEY: tuple[tuple[str, int, int], ...] | None = None
_CACHE: list[dict] = []

_QUESTION_PREFIX = re.compile(
    r'怎么(?:使用|操作|上传|新增|编辑|设置|配置|接入|连接|导入|导出|做|干|查|改|确认|处理|完成)|'
    r'如何(?:使用|操作|上传|新增|编辑|设置|配置|接入|连接|导入|导出|做|查|改|确认|处理|完成)|'
    r'使用说明|操作说明|平台说明|平台定义|使用手册|说明书|帮助|怎么用|不会用'
)
_PLATFORM_TERMS = re.compile(
    r'平台|课程|材料|文件|投放|图谱|知识点|掌握度|日历|作业|助教|Agent|agent|Skill|skill|'
    r'联网|搜索|检索|手册|说明|新增|编辑|启用|停用|导入|导出|回车|发送|设置|数据|目录|安装|分享|'
    r'整理规则|映射草稿|映射草案|[Mm][Cc][Pp]'
)
_STOP = {
    '我', '想', '请问', '问下', '一下', '帮我', '可以', '能不能', '是否', '这个', '那个',
    '怎么', '如何', '怎样', '使用', '操作', '平台', '里的', '里面', '之后', '以后', '应该',
}


def should_search(question: str) -> bool:
    """Return whether the question is about operating or understanding MinDock."""
    text = ' '.join(str(question or '').split())
    if not text or not _PLATFORM_TERMS.search(text):
        return False
    if re.search(r'手册|说明书|平台规则|操作方法', text) and re.search(
        r'怎么|如何|怎样|在哪|哪里|哪儿|步骤|做法|操作|查|改|确认|处理', text
    ):
        return True
    return bool(_QUESTION_PREFIX.search(text) or re.search(
        r'在哪里|在哪儿|是什么功能|有什么作用|为什么没有|怎么才能|能否|支持不支持|能做什么', text
    ))


def _terms(query: str) -> list[str]:
    text = ' '.join(str(query or '').split())
    terms: list[str] = []
    for word in re.findall(r'[A-Za-z][A-Za-z0-9_-]{1,}|[\u4e00-\u9fff]{2,}', text):
        if re.fullmatch(r'[\u4e00-\u9fff]+', word) and len(word) > 2:
            # Chinese questions are not whitespace tokenized. Keep adjacent
            # bigrams so "上传课程材料" can match sections containing only
            # "上传" and "课程" without introducing a large tokenizer.
            candidates = [word[i:i + 2] for i in range(len(word) - 1)]
        else:
            candidates = [word]
        for candidate in candidates:
            if candidate not in _STOP and candidate not in terms:
                terms.append(candidate)
    return terms[:12]


def _load() -> list[dict]:
    global _CACHE_KEY, _CACHE
    files = sorted(DOCS_DIR.glob('*.md')) if DOCS_DIR.is_dir() else []
    key = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    if key == _CACHE_KEY:
        return _CACHE

    chunks: list[dict] = []
    for path in files:
        lines = path.read_text(encoding='utf-8').splitlines()
        heading = path.stem
        start = 1
        body: list[str] = []
        for line_no, line in enumerate(lines + ['# __END__'], 1):
            match = re.match(r'^#{1,3}\s+(.+?)\s*$', line)
            if match:
                if body:
                    content = '\n'.join(body).strip()
                    if content:
                        chunks.append(_chunk(path, heading, start, line_no - 1, content))
                heading = match.group(1).strip()
                start = line_no
                body = []
            else:
                body.append(line)
        # Keep a useful fallback if a handout has no headings.
        if not chunks or chunks[-1]['source'].split(':', 1)[0] != str(path):
            content = '\n'.join(lines).strip()
            if content:
                chunks.append(_chunk(path, path.stem, 1, len(lines), content))
    _CACHE_KEY, _CACHE = key, chunks
    return chunks


def _chunk(path: Path, heading: str, start: int, end: int, content: str) -> dict:
    relative = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    manual_relative = path.relative_to(DOCS_DIR) if path.is_relative_to(DOCS_DIR) else Path(path.name)
    source = f'{relative}:{start}'
    return {
        'id': source,
        'kind': 'manual',
        'name': heading,
        'course_name': 'MinDock 平台',
        'chapter': heading,
        'gist': content[:1800],
        'detail': content[:6000],
        'source': source,
        'locator_type': 'markdown_line',
        'locator_start': start,
        'locator_end': end,
        'manual_path': str(manual_relative),
        'manual_url': f"/api/manual/open?path={quote(str(manual_relative), safe='')}&line={start}#line-{start}",
    }


def search(query: str, limit: int = 6) -> list[dict]:
    """Search bundled guide sections with deterministic, explainable ranking."""
    rows = _load()
    terms = _terms(query)
    normalized = ' '.join(str(query or '').split()).lower()
    ranked: list[tuple[int, dict]] = []
    for row in rows:
        haystack = f"{row['name']} {row['gist']} {row['detail']}".lower()
        score = 0
        if normalized and normalized in haystack:
            score += 12
        for term in terms:
            score += haystack.count(term.lower()) * (4 if term.lower() in row['name'].lower() else 2)
        if score:
            ranked.append((score, row))
    ranked.sort(key=lambda item: (-item[0], item[1]['source']))
    return [dict(row) for _, row in ranked[:max(1, min(int(limit), 12))]]
