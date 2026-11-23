"""Host-side web search shared by every role.

Search results are transient evidence for the current answer. The model never
gets direct network access: it requests a tool call and the StudyPal host
executes the provider or reads a public page.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
import ipaddress
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

from ..settings import store

ENDPOINT = 'https://api.tavily.com/search'
BOCHA_ENDPOINT = 'https://api.bochaai.com/v1/web-search'
RSS_ENDPOINT = 'https://www.bing.com/search?format=rss&mkt=en-US&q='
CURRENT_RE = re.compile(
    r'最新|新闻|市场|趋势|行业|互联网|搜索|检索|联网|查资料|搜集|调研|'
    r'论文|文献|引用|财报|年报|股价|股票|公司公告|研报|数据来源|资料来源'
)


def should_search(question: str, role: str, mode: str, has_agenda: bool = False) -> bool:
    if mode == 'off':
        return False
    if mode == 'on':
        return True
    # 纯作业/日程问题交给结构化日程，不必联网；但「2025 年财报」这类问题
    # 可能被日期解析器误识别成日程，仍应按资料意图放行联网。
    if re.search(r'作业|待办|课表|上课|考试|交什么|要交', question) and not re.search(r'联网|新闻|互联网|官网', question):
        return False
    return bool(CURRENT_RE.search(question) or (
        re.search(r'昨天|今天|最近|近期|现在', question) and
        re.search(r'宣布|发布|停止|训练|价格|发生|上线|消息', question)
    ))


def _tavily(query: str, limit: int, key: str, freshness: str | None = None) -> list[dict]:
    if not key:
        raise RuntimeError('未配置联网检索密钥；请到设置中填写对应搜索服务的 API Key')
    body = {'api_key': key, 'query': query, 'search_depth': 'basic',
            'max_results': max(1, min(limit, 8)), 'include_answer': False}
    if freshness in {'day', 'week', 'month', 'year'}:
        body['time_range'] = freshness
    payload = json.dumps(body).encode()
    request = urllib.request.Request(ENDPOINT, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f'联网检索失败：{exc}') from exc
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    out = []
    for result in data.get('results', []):
        url = result.get('url', '')
        if urlparse(url).scheme not in ('http', 'https'):
            continue
        out.append({'title': str(result.get('title', ''))[:200], 'url': url,
                    'snippet': str(result.get('content', ''))[:1500], 'retrieved_at': now})
    return out


def _bocha(query: str, limit: int, key: str, freshness: str | None = None) -> list[dict]:
    payload = json.dumps({
        'query': query, 'freshness': freshness or 'noLimit', 'summary': True,
        'count': max(1, min(limit, 8)),
    }).encode()
    request = urllib.request.Request(
        BOCHA_ENDPOINT, data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f'联网检索失败：{exc}') from exc
    pages = ((data.get('data') or {}).get('webPages') or {}).get('value') or []
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return [{
        'title': str(item.get('name') or '')[:200],
        'url': str(item.get('url') or ''),
        'snippet': str(item.get('summary') or item.get('snippet') or '')[:1500],
        'retrieved_at': now,
    } for item in pages if urlparse(str(item.get('url') or '')).scheme in ('http', 'https')]


class _BodyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0
        self.in_title = False
        self.title = ''
        self.links: list[dict] = []
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == 'title':
            self.in_title = True
        if tag == 'a' and not self.skip:
            href = dict(attrs).get('href')
            if href and len(self.links) < 60:
                self.links.append({'url': href})
        if tag.lower() in {'script', 'style', 'noscript', 'svg', 'nav', 'footer'}:
            self.skip += 1
    def handle_endtag(self, tag: str) -> None:
        if tag == 'title':
            self.in_title = False
        if tag.lower() in {'script', 'style', 'noscript', 'svg', 'nav', 'footer'} and self.skip:
            self.skip -= 1
    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        if not self.skip and data.strip():
            self.parts.append(data.strip())


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect before urllib opens the next URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        _public_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _public_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('网页地址必须是 http/https')
    host = parsed.hostname.lower()
    if host in {'localhost', 'localhost.localdomain'}:
        raise ValueError('不允许读取本机地址')
    try:
        addresses = [item[4][0] for item in socket.getaddrinfo(host, None)]
        if not addresses or any(not ipaddress.ip_address(addr).is_global for addr in addresses):
            raise ValueError('不允许读取内网地址')
    except socket.gaierror as exc:
        raise ValueError('网页地址无法解析') from exc
    return url


def open_page(url: str) -> dict:
    safe_url = _public_url(url)
    request = urllib.request.Request(safe_url, headers={'User-Agent': 'CyberPal/1.0'})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _SafeRedirectHandler()
    )
    try:
        with opener.open(request, timeout=20) as response:
            raw = response.read(8_000_001)
            final_url = response.geturl()
            content_type = response.headers.get_content_type()
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f'网页正文读取失败：{exc}') from exc
    _public_url(final_url)
    if len(raw) > 8_000_000:
        raise RuntimeError('资料超过 8 MB 读取限制，请下载后导入课程')
    if content_type == 'application/pdf' or raw.startswith(b'%PDF'):
        from io import BytesIO
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(raw))
        body = '\n'.join((page.extract_text() or '') for page in reader.pages[:30])[:40000]
        return {'title': (reader.metadata.title if reader.metadata else '') or final_url,
                'url': final_url, 'snippet': body[:1500], 'body': body,
                'note': '最多读取前30页及40000字符',
                'retrieved_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    if content_type not in {'text/html', 'application/xhtml+xml', 'text/plain', 'application/xml', 'text/xml'}:
        raise RuntimeError(f'网页不是 HTML 正文（{content_type}）')
    parser = _BodyParser()
    parser.feed(raw.decode('utf-8', 'replace'))
    body = re.sub(r'\s+', ' ', ' '.join(parser.parts)).strip()[:40_000]
    if not body:
        raise RuntimeError('网页正文为空或无法解析')
    return {
        'title': parser.title.strip() or final_url, 'url': final_url, 'snippet': body[:1500],
        'links': [{'url': urllib.parse.urljoin(final_url, x['url'])} for x in parser.links
                  if urlparse(urllib.parse.urljoin(final_url, x['url'])).scheme in ('http', 'https')],
        'body': body, 'retrieved_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }


def _bing_rss(query: str, limit: int = 5, freshness: str | None = None) -> list[dict]:
    url = RSS_ENDPOINT + urllib.parse.quote_plus(query)
    if freshness in {'day', 'week', 'month'}:
        url += '&filters=' + urllib.parse.quote('ex1:"' + {'day': 'ez1', 'week': 'ez2', 'month': 'ez3'}[freshness] + '"')
    request = urllib.request.Request(url, headers={'User-Agent': 'CyberPal/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            root = ET.fromstring(response.read())
    except (urllib.error.URLError, TimeoutError, ET.ParseError) as exc:
        raise RuntimeError(f'公开搜索失败：{exc}') from exc
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    out = []
    for item in root.findall('./channel/item')[:max(1, min(limit, 8))]:
        link = (item.findtext('link') or '').strip()
        if urlparse(link).scheme not in ('http', 'https'):
            continue
        out.append({'title': (item.findtext('title') or '').strip()[:200],
                    'url': link, 'snippet': (item.findtext('description') or '').strip()[:1500],
                    'retrieved_at': now, 'provider': 'bing_rss', 'query': query,
                    'date_note': 'RSS日期可能是抓取日期，发布日期需要核对原文'})
    return out


def _query_variants(query: str) -> list[str]:
    # The model owns query reformulation; the host must not drop follow-ups or years.
    query = ' '.join(str(query or '').split()).strip()[:500]
    if not query:
        return []
    if 'site:' in query or '"' in query:
        return [query]
    compact = re.sub(r'\b(?:model|release|announcement|latest|official)\b|发布|宣布|相关|最新|官方', ' ', query, flags=re.I)
    compact = re.sub(r'(?<!\d)(20\d{2})年?', r' \1 ', compact)
    compact = re.sub(r'年度报告|财报', ' 年报 ', compact)
    compact = re.sub(r'业绩|营业收入|营收|净利润|同比|增长', ' ', compact)
    compact = ' '.join(compact.split())
    quoted = ' '.join('"' + term + '"' for term in compact.split())
    return list(dict.fromkeys([query, compact, quoted]))


def _relevance(row: dict, query: str) -> float:
    terms = re.findall(r'[a-zA-Z]{2,}|20\d{2}|[\u4e00-\u9fff]{2,}', query.lower())
    tokens = []
    for term in terms:
        if re.fullmatch(r'[\u4e00-\u9fff]+', term) and len(term) > 2:
            tokens.extend(term[i:i+2] for i in range(len(term)-1))
        else:
            tokens.append(term)
    text = ' '.join(str(row.get(k) or '') for k in ('title', 'snippet', 'url')).lower()
    return sum(token in text for token in tokens) / max(1, len(tokens))


def search(query: str, limit: int = 5, freshness: str | None = None) -> list[dict]:
    """Search a configured provider without silently discarding query terms."""
    variants = _query_variants(query)
    if not variants:
        raise RuntimeError('搜索词为空')
    query = variants[0]
    cfg = store.read_raw().get('web', {})
    key = (cfg.get('api_key') or '').strip()
    provider = cfg.get('provider') or 'bing_rss'
    if provider in {'tavily', 'bocha'} and key:
        fn = _tavily if provider == 'tavily' else _bocha
        rows = fn(query, limit, key, freshness)
        for row in rows:
            row.update(provider=provider, query=query)
        return rows
    rows = {}
    errors = []
    for variant in variants[:3]:
        try:
            found = _bing_rss(variant, limit, freshness)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        for row in found:
            row['relevance'] = _relevance(row, variants[1] if len(variants) > 1 else query)
            rows.setdefault(row['url'], row)
        if any(row['relevance'] >= 0.8 for row in rows.values()):
            break
    ranked = sorted(rows.values(), key=lambda row: row['relevance'], reverse=True)
    # Do not pass dictionary/homepage noise off as evidence for a specific event.
    ranked = [r for r in ranked if r['relevance'] >= 0.55]
    if not ranked and errors and not rows:
        raise RuntimeError('; '.join(errors))
    return ranked[:max(1, min(limit, 8))]
