"""Material reading (B3/T3.5): source metadata, evidence blocks, page images.

Read-only module for the "evidence click -> original text" path. source_files.path
is stored as an absolute path by the scanner; every path is re-resolved against
db.DATA_DIR here, so a compromised or stale row can never make the API read an
arbitrary file. Page images are best effort: pypdfium2 when the runtime ships it,
otherwise the PDF text layer is drawn into a PNG; a page without a text layer
raises UnsupportedPageRender so the route can fall back to plain text (415).

Blocks are evidence rows (never source_chunks): every block keeps its evidence id
and locator, and stale rows from a rescan are hidden. Only evidence therefore
counts as a citable original, which keeps the reader aligned with the kp evidence.
"""

from __future__ import annotations

import io
from pathlib import Path

from . import db
from .db import connect

MAX_BLOCK_CHARS = 20_000
DEFAULT_BLOCK_LIMIT = 50
MAX_BLOCK_LIMIT = 200
MAX_CONTEXT = 10
MIN_RENDER_DPI = 72
MAX_RENDER_DPI = 200
DEFAULT_RENDER_DPI = 144
_MAX_IMAGE_SIDE = 10_000
_FONT_CANDIDATES = (
    '/System/Library/Fonts/STHeiti Light.ttc',
    '/System/Library/Fonts/Supplemental/Songti.ttc',
    '/System/Library/Fonts/PingFang.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    'C:/Windows/Fonts/msyh.ttc',
    'C:/Windows/Fonts/simhei.ttf',
    'C:/Windows/Fonts/simsun.ttc',
)


class MaterialError(ValueError):
    """Bad locator or unsupported read request (mapped to HTTP 400)."""


class MaterialNotFound(MaterialError):
    """The material/evidence row or its original file is gone (HTTP 404)."""


class UnsupportedPageRender(MaterialError):
    """The page cannot be rasterised here; the caller falls back to text."""

    fallback = 'text'


def _data_root() -> Path:
    return Path(db.DATA_DIR).expanduser().resolve()


def _resolve_data_path(raw: str) -> Path:
    """Resolve a stored source path and require it to stay inside DATA_DIR."""
    text = str(raw or '').strip()
    if not text:
        raise MaterialNotFound('材料文件路径为空，请重新扫描材料')
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = _data_root() / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(_data_root())
    except (OSError, RuntimeError, ValueError) as exc:
        raise MaterialError('材料文件不在数据目录内，已拒绝读取') from exc
    if not resolved.is_file():
        raise MaterialNotFound('原材料文件已不存在，请恢复文件并重新扫描')
    return resolved


def _source_row(conn, source_id: int):
    row = conn.execute(
        '''SELECT sf.id, sf.path, sf.source_kind, sf.status, sf.course_id,
                  co.name AS course_name
           FROM source_files sf
           LEFT JOIN courses co ON co.id = sf.course_id
           WHERE sf.id = ?''',
        (source_id,),
    ).fetchone()
    if not row:
        raise MaterialNotFound('材料不存在')
    return row


def _source_meta(conn, source_id: int) -> tuple[dict, Path]:
    row = _source_row(conn, source_id)
    resolved = _resolve_data_path(row['path'])
    locator_types = [
        item['locator_type'] for item in conn.execute(
            '''SELECT DISTINCT locator_type FROM evidence
               WHERE source_id = ? AND COALESCE(stale, 0) = 0
               ORDER BY locator_type''',
            (row['id'],),
        ).fetchall()
    ]
    counts = conn.execute(
        '''SELECT
             (SELECT COUNT(*) FROM source_chunks
               WHERE source_id = ? AND COALESCE(stale, 0) = 0) AS chunk_count,
             (SELECT COUNT(*) FROM evidence
               WHERE source_id = ? AND COALESCE(stale, 0) = 0) AS evidence_count,
             (SELECT COUNT(*) FROM evidence WHERE source_id = ?) AS total_evidence''',
        (row['id'], row['id'], row['id']),
    ).fetchone()
    meta = {
        'id': row['id'],
        'path': row['path'],
        'source_kind': row['source_kind'] or '',
        'locator_types': locator_types,
        'chunk_count': counts['chunk_count'],
        'evidence_count': counts['evidence_count'],
        'total_evidence': counts['total_evidence'],
    }
    return meta, resolved


def resolve_source(source_id: int) -> dict:
    """Metadata for one material; 404s when the row or its file is missing."""
    conn = connect()
    try:
        meta, _ = _source_meta(conn, source_id)
    finally:
        conn.close()
    return meta


def _split_locator(value):
    """Accept '12' or 'page:12'; the prefix is only a locator_type hint."""
    text = str(value if value is not None else '').strip()
    if not text:
        return None, None
    kind = None
    if ':' in text:
        kind, _, text = text.partition(':')
        kind = kind.strip() or None
        text = text.strip()
    if not text.isdigit():
        raise MaterialError('定位需要形如 12 或 page:12 的定位')
    return kind, int(text)


def _block(row) -> dict:
    text = row['content'] or ''
    return {
        'evidence_id': row['id'],
        'locator_type': row['locator_type'],
        'locator_start': row['locator_start'],
        'locator_end': row['locator_end'],
        'text': text[:MAX_BLOCK_CHARS],
        'truncated': len(text) > MAX_BLOCK_CHARS,
    }


def _clamp_limit(limit) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise MaterialError('limit 需要是整数') from exc
    return max(1, min(value, MAX_BLOCK_LIMIT))


def blocks(source_id: int, from_locator=None, limit=DEFAULT_BLOCK_LIMIT,
           locator_type: str | None = None) -> dict:
    """Evidence blocks ordered by locator, one page at a time."""
    limit = _clamp_limit(limit)
    version_type, start = _split_locator(from_locator)
    wanted_type = (str(locator_type or '').strip() or version_type or None)
    conn = connect()
    try:
        _source_meta(conn, source_id)
        sql = ('SELECT id, locator_type, locator_start, locator_end, content '
               'FROM evidence WHERE source_id = ? AND COALESCE(stale, 0) = 0')
        params: list = [source_id]
        if wanted_type:
            sql += ' AND locator_type = ?'
            params.append(wanted_type)
        if start is not None:
            sql += ' AND locator_start IS NOT NULL AND locator_start >= ?'
            params.append(start)
        sql += ' ORDER BY COALESCE(locator_start, 0), id LIMIT ?'
        params.append(limit + 1)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    has_more = len(rows) > limit
    return {'blocks': [_block(row) for row in rows[:limit]], 'has_more': has_more}


def _context_count(value, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise MaterialError(f'{name} 需要是整数') from exc
    return max(0, min(number, MAX_CONTEXT))


def evidence_context(evidence_id: int, before=1, after=1) -> dict:
    """The clicked evidence plus up to N neighbouring blocks of the same source."""
    before = _context_count(before, 'before')
    after = _context_count(after, 'after')
    conn = connect()
    try:
        target = conn.execute(
            '''SELECT id, source_id, snapshot_id, locator_type, locator_start,
                      locator_end, content
               FROM evidence WHERE id = ? AND COALESCE(stale, 0) = 0''',
            (evidence_id,),
        ).fetchone()
        if not target:
            raise MaterialNotFound('证据不存在')
        meta, _ = _source_meta(conn, target['source_id'])
        # 过期/软删证据不参与阅读上下文：target 与邻居统一叠加 stale 过滤，
        # 避免 snapshot 分支绕过该约定后仍能读到旧证据。
        where = 'source_id = ? AND id <> ? AND COALESCE(stale, 0) = 0'
        params: list = [target['source_id'], target['id']]
        if target['snapshot_id'] is not None:
            where += ' AND snapshot_id IS ?'
            params.append(target['snapshot_id'])
        anchor = int(target['locator_start'] or 0)
        before_rows = conn.execute(
            f'''SELECT id, locator_type, locator_start, locator_end, content
                FROM evidence WHERE {where}
                  AND (COALESCE(locator_start, 0) < ?
                       OR (COALESCE(locator_start, 0) = ? AND id < ?))
                ORDER BY COALESCE(locator_start, 0) DESC, id DESC LIMIT ?''',
            [*params, anchor, anchor, target['id'], before],
        ).fetchall()
        after_rows = conn.execute(
            f'''SELECT id, locator_type, locator_start, locator_end, content
                FROM evidence WHERE {where}
                  AND (COALESCE(locator_start, 0) > ?
                       OR (COALESCE(locator_start, 0) = ? AND id > ?))
                ORDER BY COALESCE(locator_start, 0) ASC, id ASC LIMIT ?''',
            [*params, anchor, anchor, target['id'], after],
        ).fetchall()
    finally:
        conn.close()
    meta.pop('total_evidence', None)
    return {
        'evidence': _block(target),
        'before': [_block(row) for row in reversed(before_rows)],
        'after': [_block(row) for row in after_rows],
        'source': meta,
    }


def _clamp_dpi(dpi) -> int:
    try:
        value = int(dpi)
    except (TypeError, ValueError) as exc:
        raise MaterialError('dpi 需要是整数') from exc
    return max(MIN_RENDER_DPI, min(value, MAX_RENDER_DPI))


def _page_number(locator) -> int:
    kind, number = _split_locator(locator)
    if number is None:
        return 1
    if kind and kind != 'page':
        raise MaterialError('页码定位需要形如 page:3')
    if number < 1:
        raise MaterialError('页码从 1 开始')
    return number


def _raster_with_pypdfium2(path: Path, page_index: int, dpi: int):
    """Real rasterisation when the optional pypdfium2 wheel is installed."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    try:
        document = pdfium.PdfDocument(str(path))
        try:
            page = document[page_index]
            try:
                image = page.render(scale=dpi / 72.0).to_pil()
            finally:
                page.close()
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            return buffer.getvalue()
        finally:
            document.close()
    except Exception:
        # Any pypdfium2 failure degrades to the text-layer renderer below.
        return None


def _load_font(ImageFont, size: int):
    for candidate in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size=size)
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap_text(draw, text: str, font, max_width: float) -> list[str]:
    lines: list[str] = []
    for paragraph in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if not paragraph:
            lines.append('')
            continue
        current = ''
        for char in paragraph:
            candidate = current + char
            try:
                measured = draw.textlength(candidate, font=font)
            except (AttributeError, TypeError):
                measured = len(candidate) * 8
            if current and measured > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines


def _render_text_page(text: str, page_width: float, page_height: float, dpi: int) -> bytes:
    """Draw the PDF text layer into a PNG (no rasteriser available)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise UnsupportedPageRender(
            '当前运行环境缺少图像库，无法渲染页图，请阅读文本块') from exc
    scale = dpi / 72.0
    width = max(1, min(int(round(page_width * scale)), _MAX_IMAGE_SIDE))
    height = max(1, min(int(round(page_height * scale)), _MAX_IMAGE_SIDE))
    margin = max(8, int(round(36 * scale)))
    font_size = max(11, int(round(11 * scale)))
    font = _load_font(ImageFont, font_size)
    line_height = max(font_size + 2, int(round(font_size * 1.45)))
    image = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(image)
    lines = _wrap_text(draw, text, font, max(1, width - 2 * margin))
    y = margin
    for line in lines:
        if y + line_height > height - margin:
            break
        draw.text((margin, y), line, fill='black', font=font)
        y += line_height
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def render_page_png(source_id: int, locator=None, dpi=DEFAULT_RENDER_DPI) -> bytes:
    """One-page PNG for a PDF material; 415-style errors for everything else."""
    conn = connect()
    try:
        _meta, path = _source_meta(conn, source_id)
    finally:
        conn.close()
    if path.suffix.lower() != '.pdf':
        raise UnsupportedPageRender('只有 PDF 材料支持页图，请阅读文本块')
    page = _page_number(locator)
    dpi = _clamp_dpi(dpi)
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise UnsupportedPageRender(
            '当前运行环境缺少 PDF 解析库，无法渲染页图，请阅读文本块') from exc
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt('')
            except Exception as exc:
                raise UnsupportedPageRender(
                    'PDF 已加密，无法渲染页图，请阅读文本块') from exc
        total = len(reader.pages)
    except UnsupportedPageRender:
        raise
    except Exception as exc:
        raise MaterialError('PDF 无法解析，请重新扫描材料') from exc
    if page > total:
        raise MaterialError(f'页码超出范围：材料共 {total} 页')
    # The API renders one page per request, so the "<= 50 pages" guard is
    # structural: a client cannot ask for a range and no cache is written.
    png = _raster_with_pypdfium2(path, page - 1, dpi)
    if png:
        return png
    pdf_page = reader.pages[page - 1]
    try:
        media = pdf_page.mediabox
        page_width = float(media.width) or 612.0
        page_height = float(media.height) or 792.0
    except (AttributeError, TypeError, ValueError):
        page_width, page_height = 612.0, 792.0
    try:
        text = (pdf_page.extract_text() or '').strip()
    except Exception:
        text = ''
    if not text:
        raise UnsupportedPageRender(
            '该 PDF 页面没有文本层，当前运行环境无法渲染页图，请阅读文本块')
    return _render_text_page(text, page_width, page_height, dpi)
