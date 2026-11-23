"""Canonical textbook structure used by the graph navigation layer.

The old graph used ``kp.chapter`` as both display text and hierarchy. This
module keeps chapter structure independent from knowledge nodes while still
providing conservative helpers for legacy records.
"""

from __future__ import annotations

import re
from typing import Any


_CN_DIGITS = {
    '零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
    '百': 100,
}

CATALOG_VERSION = '2'


def parse_number(value: str) -> int | None:
    text = str(value or '').strip()
    if text.isdigit():
        return int(text)
    if not text or not all(char in _CN_DIGITS for char in text):
        return None
    if text == '十':
        return 10
    if len(text) == 2 and text[0] == '十':
        return 10 + _CN_DIGITS[text[1]]
    if len(text) == 2 and text[1] == '十':
        return _CN_DIGITS[text[0]] * 10
    if len(text) == 3 and text[1] == '十':
        return _CN_DIGITS[text[0]] * 10 + _CN_DIGITS[text[2]]
    if len(text) == 2 and text[0] == '百':
        return 100 + _CN_DIGITS[text[1]]
    return _CN_DIGITS.get(text)


def chapter_key(number: int) -> str:
    return f'ch-{int(number):02d}'


def section_key(number: int, section_number: str) -> str:
    parts = re.findall(r'\d+', str(section_number))
    safe = '-'.join(f'{int(part):02d}' for part in parts[1:]) if len(parts) > 1 else '00'
    return f'{chapter_key(number)}-sec-{safe}'


def chapter_number(value: str) -> int | None:
    match = re.search(r'第\s*([0-9０-９一二三四五六七八九十百两]+)\s*章', str(value or ''))
    if not match:
        match = re.search(r'(^|\s)([0-9]{1,2})\s*章', str(value or ''))
    if not match:
        return None
    token = match.group(1) if match.lastindex == 1 else match.group(2)
    return parse_number(token.translate(str.maketrans('０１２３４５６７８９', '0123456789')))


def _chapter_title(text: str, number: int) -> str:
    compact = re.sub(r'\s+', '', text)
    pattern = re.compile(rf'第\s*{number}\s*章\s*(.{{0,100}}?)(?:学习目的|本章小结|$)')
    match = pattern.search(compact)
    if match:
        return re.sub(r'[：:。．·…]+$', '', match.group(1)).strip()
    return ''


def _sections(number: int, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    pattern = re.compile(r'^(\d{1,2}(?:\s*[.．]\s*\d{1,2}){1,3})\s+(.{2,120})$')
    for row in rows:
        text = str(row.get('text') or '')
        for raw_line in text.splitlines():
            line = re.sub(r'\s+', ' ', raw_line).strip()
            match = pattern.match(line)
            if not match:
                continue
            subsection = re.sub(r'\s*[.．]\s*', '.', match.group(1))
            parts = subsection.split('.')
            if int(parts[0]) != number:
                continue
            title = match.group(2).strip(' 。．:：')
            # PDF extraction sometimes leaves spaces between Chinese glyphs.
            title = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', title)
            # Ignore prose that happens to begin with a section-like number.
            if len(title) < 2 or title.startswith(('节将', '章将', '元/股', '版')):
                continue
            found.setdefault(subsection, {
                'key': section_key(number, subsection),
                'number': subsection,
                'title': title[:120].strip(),
                'start_locator': row.get('locator_start'),
                'end_locator': row.get('locator_end'),
            })
    sections = sorted(found.values(), key=lambda item: tuple(int(part) for part in item['number'].split('.')))
    for index, section in enumerate(sections[:-1]):
        next_start = sections[index + 1].get('start_locator')
        start = section.get('start_locator')
        if isinstance(start, int) and isinstance(next_start, int):
            section['end_locator'] = max(start, next_start - 1)
    return sections


def extract_catalog(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract chapters from real page/line evidence and return them ordered."""
    starts: list[tuple[int, int, str]] = []
    pattern = re.compile(r'第\s*([0-9０-９一二三四五六七八九十百两]+)\s*章')
    for index, item in enumerate(evidence):
        text = str(item.get('text') or '')
        compact = re.sub(r'\s+', '', text)
        match = pattern.search(compact)
        if not match:
            continue
        if '学习目的' not in compact:
            continue
        number = parse_number(match.group(1).translate(str.maketrans('０１２３４５６７８９', '0123456789')))
        if number is None:
            continue
        title = _chapter_title(text, number)
        if not title:
            title_match = re.search(rf'第\s*{number}\s*章\s*([^\n]{0,100})', text)
            title = re.sub(r'\s+', ' ', title_match.group(1)).strip(' 。．:：') if title_match else ''
        if not any(existing[0] == number for existing in starts):
            starts.append((number, index, title))
    result = []
    for index, (number, start_index, title) in enumerate(starts):
        end_index = starts[index + 1][1] if index + 1 < len(starts) else len(evidence)
        rows = evidence[start_index:end_index]
        locators = [row.get('locator_start') for row in rows if row.get('locator_start') is not None]
        result.append({
            'key': chapter_key(number),
            'number': number,
            'title': title,
            'start_locator': min(locators) if locators else None,
            'end_locator': max(locators) if locators else None,
            'catalog_version': CATALOG_VERSION,
            'sections': _sections(number, rows),
        })
    return sorted(result, key=lambda item: item['number'])


def is_structural_node(node: dict[str, Any], source_kind: str = '') -> bool:
    """Return true for chapter headings accidentally imported as knowledge.

    A heading from a course catalog or textbook is navigation structure. A
    heading-like title from a class note can still be a user-approved
    knowledge node, so provenance must take part in this decision.
    """
    raw_name = str(node.get('name') or '').strip()
    name = re.sub(r'\s+', '', raw_name)
    chapter = str(node.get('chapter') or '').strip()
    if chapter != '来源材料':
        return False
    if re.fullmatch(r'第[0-9０-９一二三四五六七八九十百两]+章', name):
        return True
    if len(name) > 40 and name.startswith('第') and ('章' in name or '教材' in name):
        return True
    if source_kind in {'class_note', 'supplement', 'event_capture'}:
        return False
    return bool(
        re.match(r'^第[0-9０-９一二三四五六七八九十百两]+章', name)
    )


def legacy_chapter_key(value: str) -> str:
    number = chapter_number(value)
    return chapter_key(number) if number is not None else ''
