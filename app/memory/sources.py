"""Index user-approved local course folders without changing the curated knowledge graph."""

from __future__ import annotations

import hashlib
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from shutil import copy2
from functools import wraps
from threading import RLock

from ..ingest.control import IngestCancelled, checkpoint, run as run_extractor

from .db import connect
from . import domain_profiles
from .knowledge import normalize_candidate
from . import wiki

SUPPORTED = {'.txt', '.md', '.markdown', '.csv', '.pdf', '.docx', '.pptx',
             '.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff',
             '.wav', '.mp3', '.m4a', '.aac', '.flac', '.ogg'}
OCR_FORMATS = {'.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff', '.pdf'}
AUDIO_FORMATS = {'.wav', '.mp3', '.m4a', '.aac', '.flac', '.ogg'}
# Course textbooks are commonly large scanned PDFs; keep the limit explicit
# while allowing a single local textbook to enter the pipeline.
MAX_BYTES = 200 * 1024 * 1024
# Uploads and extraction have different limits. A large source can remain a
# valid local archive while extraction is deferred to avoid a pypdf memory spike.
MAX_PDF_EXTRACTION_BYTES = 100 * 1024 * 1024
# Bounded chunk retention for very large materials. Callers must surface the
# boolean from _chunks() as an explicit truncated flag instead of silently
# dropping the tail of a 200 MB textbook.
MAX_SOURCE_CHUNKS = 500
CHUNK_SIZE = 1200
_scan_lock = RLock()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checkpoint()
            digest.update(chunk)
    return digest.hexdigest()


def _serialized_scan(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        while not _scan_lock.acquire(timeout=0.1):
            checkpoint()
        try:
            checkpoint()
            return fn(*args, **kwargs)
        finally:
            _scan_lock.release()
    return wrapped

SOURCE_KINDS = {
    'textbook', 'class_note', 'supplement', 'course_catalog',
    'event_capture', 'development_doc', 'wiki_page', 'unclassified',
}


def normalize_source_kind(value: str | None) -> str:
    value = str(value or '').strip().lower()
    return value if value in SOURCE_KINDS else 'unclassified'


def source_metadata(kind: str | None, role: str | None = '') -> dict:
    """Return stable metadata for the ingest contract without guessing user intent."""
    source_kind = normalize_source_kind(kind)
    defaults = {
        'course_catalog': ('catalog', 'candidate'),
        'textbook': ('primary_material', 'none'),
        'class_note': ('material', 'none'),
        'supplement': ('material', 'none'),
        'event_capture': ('event_source', 'none'),
        'development_doc': ('platform_document', 'none'),
        'wiki_page': ('wiki_page', 'none'),
        'unclassified': ('material', 'none'),
    }
    default_role, catalog_status = defaults[source_kind]
    source_role = str(role or '').strip() or default_role
    return {'source_kind': source_kind, 'source_role': source_role,
            'catalog_status': catalog_status}


class ExtractionRequiredError(ValueError):
    """The file is stored, but a text/OCR extractor is required before indexing."""


def _tesseract_languages(executable: str | None) -> set[str]:
    if not executable:
        return set()
    try:
        output = run_extractor([executable, '--list-langs'], capture_output=True,
                                text=True, check=False, timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.strip() for line in output.splitlines()[1:] if line.strip()}


def extractor_status() -> dict:
    """Report local extractors so the UI can distinguish indexed from readable."""
    tesseract = shutil.which('tesseract')
    whisper = shutil.which('whisper') or shutil.which('whisper-cli')
    languages = _tesseract_languages(tesseract)
    native = _native_ocr()
    return {
        'tesseract': bool(tesseract),
        'tesseract_languages': sorted(languages),
        'chinese_ocr': bool(native) or 'chi_sim' in languages or 'chi_tra' in languages,
        'native_ocr': bool(native),
        'whisper': bool(whisper),
    }


def _native_ocr() -> str | None:
    executable = os.environ.get('MINDOCK_OCR', '')
    python = os.environ.get('MINDOCK_OCR_PYTHON', '')
    if python:
        return executable if executable and Path(executable).is_file() and Path(python).is_file() else None
    return executable if executable and Path(executable).is_file() and os.access(executable, os.X_OK) else None


def _native_ocr_text(path: Path, *, pdf: bool = False):
    executable = _native_ocr()
    if not executable:
        raise ExtractionRequiredError('内置文字识别不可用')
    try:
        checkpoint('ocr')
        python = os.environ.get('MINDOCK_OCR_PYTHON', '')
        command = [python, executable] if python else [executable]
        result = run_extractor(command + ['--pdf' if pdf else '--image', str(path)],
                                capture_output=True, text=True, timeout=600 if pdf else 120)
        if result.returncode or not result.stdout.strip():
            raise ExtractionRequiredError('未识别到文字，请检查材料清晰度')
        return json.loads(result.stdout) if pdf else result.stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ExtractionRequiredError(f'内置文字识别失败：{exc}') from exc


def _tesseract_text(image: Path) -> str:
    if _native_ocr():
        return _native_ocr_text(image)
    executable = shutil.which('tesseract')
    if not executable:
        raise ExtractionRequiredError('需要安装 tesseract 才能识别图片或扫描版 PDF')
    languages = _tesseract_languages(executable)
    if not ({'chi_sim', 'chi_tra'} & languages):
        raise ExtractionRequiredError('本机缺少中文 OCR 语言包（chi_sim/chi_tra），图片已保存但暂不能可靠提取中文')
    lang = 'chi_sim+eng' if 'chi_sim' in languages else 'chi_tra+eng'
    try:
        proc = run_extractor([executable, str(image), 'stdout', '-l', lang, '--psm', '3'],
                              capture_output=True, text=True, check=False, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExtractionRequiredError(f'OCR 执行失败：{exc}') from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ExtractionRequiredError('OCR 没有识别出文字，请检查图片清晰度或安装中文语言包')
    return proc.stdout.strip()


DOCX_IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff', '.bmp', '.gif'}
MAX_DOCX_OCR_IMAGES = 40


def _docx_embedded_images(path: Path) -> list[tuple[str, bytes]]:
    """Embedded pictures of a DOCX package (word/media/*) as (suffix, blob).

    A DOCX is a zip container, so image-only documents are recognizable without
    any conversion step. The old DOCX OCR branch wrongly ran pdftoppm on the
    .docx file itself; that can never produce pages and silently indexed an
    empty document (O-REL-4).
    """
    images: list[tuple[str, bytes]] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for name in sorted(archive.namelist()):
                if not name.startswith('word/media/'):
                    continue
                suffix = Path(name).suffix.lower()
                if suffix not in DOCX_IMAGE_SUFFIXES:
                    continue
                try:
                    blob = archive.read(name)
                except (KeyError, OSError):
                    continue
                if blob:
                    images.append((suffix, blob))
                if len(images) >= MAX_DOCX_OCR_IMAGES:
                    break
    except (OSError, zipfile.BadZipFile):
        return []
    return images


def _docx_image_ocr(path: Path, images: list[tuple[str, bytes]]) -> list[str]:
    """OCR the embedded images of an image-only DOCX and label the source pages."""
    if not images:
        raise ExtractionRequiredError('DOCX 没有文字层，也没有可识别的内嵌图片')
    if not _native_ocr() and not shutil.which('tesseract'):
        raise ExtractionRequiredError(
            f'DOCX 只有图片、没有文字层（共 {len(images)} 张图），需要本机 OCR 才能建立知识索引')
    pages: list[str] = []
    with tempfile.TemporaryDirectory(prefix='cyberpal-docx-') as tmp:
        for index, (suffix, blob) in enumerate(images, 1):
            image = Path(tmp) / f'image-{index:03d}{suffix}'
            image.write_bytes(blob)
            try:
                recognized = _tesseract_text(image).strip()
            except ExtractionRequiredError:
                continue
            if recognized:
                pages.append(recognized)
    if not pages:
        raise ExtractionRequiredError(
            'DOCX 只有图片，OCR 没有识别出可用文字，请检查图片清晰度或安装中文 OCR 语言包')
    return pages


def _audio_sidecar(path: Path) -> Path | None:
    for suffix in ('.txt', '.md'):
        candidate = path.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return None


def _transcription_text(path: Path) -> str:
    sidecar = _audio_sidecar(path)
    if sidecar:
        return sidecar.read_text(encoding='utf-8-sig', errors='replace').strip()
    whisper = shutil.which('whisper') or shutil.which('whisper-cli')
    if not whisper:
        raise ExtractionRequiredError(
            '音频已归档，但需要本机 Whisper/whisper-cli 转写；也可以把同名 .txt 或 .md 放在旁边后重新扫描'
        )
    with tempfile.TemporaryDirectory(prefix='cyberpal-whisper-') as tmp:
        try:
            proc = run_extractor([whisper, str(path), '--output_dir', tmp, '--output_format', 'txt'],
                                  capture_output=True, text=True, check=False, timeout=900)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExtractionRequiredError(f'转写执行失败：{exc}') from exc
        output = Path(tmp) / f'{path.stem}.txt'
        if proc.returncode != 0 or not output.is_file():
            raise ExtractionRequiredError('转写失败，请检查 Whisper 模型或音频格式')
        return output.read_text(encoding='utf-8', errors='replace').strip()


def extract_structured(path: Path) -> tuple[str, list[str], list[dict]]:
    checkpoint('extracting')
    suffix = path.suffix.lower()
    headings: list[str] = []
    evidence: list[dict] = []
    if suffix in {'.txt', '.md', '.markdown', '.csv'}:
        text = path.read_text(encoding='utf-8-sig', errors='replace')
        if suffix in {'.md', '.markdown'}:
            headings = [m.group(1).strip() for m in re.finditer(r'^#{1,4}\s+(.+)$', text, re.M)]
        for number, line in enumerate(text.splitlines(), 1):
            if line.strip():
                evidence.append({'locator_type': 'line', 'locator_start': number,
                                 'locator_end': number, 'text': line.strip()})
    elif suffix == '.pdf':
        from pypdf import PdfReader
        document = PdfReader(str(path))
        pages = []
        for number, page in enumerate(document.pages, 1):
            checkpoint('parsing', number - 1, len(document.pages))
            pages.append(page.extract_text() or '')
            checkpoint('parsing', number, len(document.pages))
        text = '\n'.join(pages)
        evidence = [{'locator_type': 'page', 'locator_start': number, 'locator_end': number,
                     'text': page.strip()} for number, page in enumerate(pages, 1) if page.strip()]
        # P2 (T2.11): a real text layer is text material even when short. Only
        # a truly empty text layer is a scanned PDF worth OCR.
        if _native_ocr() and not re.sub(r'\s+', '', text):
            recognized = _native_ocr_text(path, pdf=True)
            text = '\n'.join(page['text'] for page in recognized)
            evidence = [{'locator_type': 'page_ocr', 'locator_start': page['page'],
                         'locator_end': page['page'], 'text': page['text']} for page in recognized]
        chapter_headings = re.findall(r'^第[一二三四五六七八九十百零0-9]+章[^\n]{0,100}', text, re.M)
        headings.extend(item.strip() for item in chapter_headings if item.strip())
    elif suffix == '.docx':
        from docx import Document
        doc = Document(str(path))
        text = '\n'.join(p.text for p in doc.paragraphs)
        headings = [p.text.strip() for p in doc.paragraphs if p.style and p.style.name.startswith('Heading') and p.text.strip()]
        evidence = [{'locator_type': 'paragraph', 'locator_start': number, 'locator_end': number,
                     'text': paragraph.text.strip()} for number, paragraph in enumerate(doc.paragraphs, 1)
                    if paragraph.text.strip()]
        for table in doc.tables:
            text += '\n' + '\n'.join(' | '.join(c.text for c in row.cells) for row in table.rows)
        images = _docx_embedded_images(path)
        if len(re.sub(r'\s+', '', text)) < 2000 and images:
            # A DOCX is not a PDF: rasterizing it with pdftoppm can never work
            # and used to silently index an empty document. OCR the embedded
            # pictures directly and label the recognized pages as image_ocr.
            pages = _docx_image_ocr(path, images)
            text = (text + '\n' + '\n'.join(pages)).strip()
            evidence = evidence + [
                {'locator_type': 'image_ocr', 'locator_start': number,
                 'locator_end': number, 'text': page.strip()}
                for number, page in enumerate(pages, 1) if page.strip()]
        if not text.strip() and not evidence:
            raise ExtractionRequiredError(
                'DOCX 没有可提取的文字层；请使用带文字层的文档或安装本机 OCR 后重试')
    elif suffix in OCR_FORMATS - {'.pdf'}:
        text = _tesseract_text(path)
        evidence = [{'locator_type': 'ocr', 'locator_start': 1, 'locator_end': 1, 'text': text}]
    elif suffix in AUDIO_FORMATS:
        text = _transcription_text(path)
        evidence = [{'locator_type': 'transcript', 'locator_start': 1, 'locator_end': 1, 'text': text}]
    elif suffix == '.pptx':
        from pptx import Presentation
        slides = Presentation(str(path)).slides
        slide_text = ['\n'.join(shape.text for shape in slide.shapes if shape.has_text_frame).strip()
                      for slide in slides]
        text = '\n'.join(slide_text)
        evidence = [{'locator_type': 'slide', 'locator_start': number, 'locator_end': number,
                     'text': slide} for number, slide in enumerate(slide_text, 1) if slide]
        headings = [slide.shapes.title.text.strip() for slide in slides if slide.shapes.title and slide.shapes.title.text.strip()]
    else:
        raise ValueError('暂不支持该格式的文字抽取')
    cleaned = text.strip()
    # P2 (T2.11): short-but-present text is still a text PDF; only a blank text
    # layer means "scanned image, OCR required".
    if (suffix == '.pdf'
            and not any(item['locator_type'] == 'page_ocr' for item in evidence)
            and not re.sub(r'\s+', '', cleaned)):
        raise ExtractionRequiredError('PDF 没有文本层，需要 OCR 后才能建立知识索引')
    return cleaned, list(dict.fromkeys(headings))[:100], evidence


def extract(path: Path) -> tuple[str, list[str]]:
    """Backward-compatible text extraction API."""
    text, headings, _ = extract_structured(path)
    return text, headings


def chapter_sections(evidence: list[dict]) -> list[dict]:
    """Find real textbook chapter starts from page evidence, not table-of-contents rows."""
    starts: list[dict] = []
    pattern = re.compile(r'第\s*([0-9０-９]+)\s*章\s*(.{0,80}?)学习目的')
    for index, item in enumerate(evidence):
        compact = re.sub(r'\s+', '', str(item.get('text') or ''))
        match = pattern.search(compact)
        if not match:
            continue
        number = int(match.group(1).translate(str.maketrans('０１２３４５６７８９', '0123456789')))
        title = re.sub(r'[：:。．·…]+$', '', match.group(2)).strip()
        starts.append({'number': number, 'chapter': f'第{number}章',
                       'title': title, 'start_index': index})
    starts = [item for index, item in enumerate(starts)
              if index == 0 or item['number'] != starts[index - 1]['number']]
    sections = []
    for index, item in enumerate(starts):
        end = starts[index + 1]['start_index'] if index + 1 < len(starts) else len(evidence)
        rows = [dict(row) for row in evidence[item['start_index']:end]]
        chapter = f"{item['chapter']} {item['title']}".strip()
        sections.append({'chapter': chapter, 'number': item['number'],
                         'title': item['title'], 'evidence': rows,
                         'content': '\n'.join(row.get('text', '') for row in rows)})
    return sections


def _structural_heading(value: str) -> bool:
    compact = re.sub(r'\s+', '', str(value or ''))
    return bool(re.match(r'^第[0-9０-９一二三四五六七八九十百]+章', compact))


def _chunks(text: str, size: int = CHUNK_SIZE) -> tuple[list[str], bool]:
    """Split extracted text into overlapping chunks and report an explicit cap.

    Character overlap preserves sentences crossing a boundary. The cap keeps a
    large textbook from producing unbounded rows; the boolean tells the scan
    caller that the tail was not indexed (evidence rows stay complete).
    """
    chunks = [text[i:i + size] for i in range(0, len(text), size - 150)][:MAX_SOURCE_CHUNKS + 1]
    truncated = len(chunks) > MAX_SOURCE_CHUNKS
    return chunks[:MAX_SOURCE_CHUNKS], truncated


def _source_snapshot(conn, source_id: int, digest: str) -> int:
    """022: idempotently record one extraction snapshot per source digest."""
    conn.execute('INSERT OR IGNORE INTO source_snapshots(source_id,sha256) VALUES(?,?)',
                 (source_id, digest))
    return conn.execute(
        'SELECT id FROM source_snapshots WHERE source_id=? AND sha256=?',
        (source_id, digest)).fetchone()['id']


def _retire_stale_source_rows(conn, source_id: int) -> None:
    """022: keep prior extraction rows as stale history instead of deleting.

    Deleting chunks/evidence also cascaded into candidate_reviews and
    kp_evidence; stale rows keep those links resolvable while read paths filter
    them out of the active view.
    """
    conn.execute('UPDATE source_chunks SET stale=1 WHERE source_id=? AND stale=0', (source_id,))
    conn.execute('UPDATE evidence SET stale=1 WHERE source_id=? AND stale=0', (source_id,))
    # 011's update triggers re-index on every UPDATE, so drop the retired rows
    # explicitly: stale history must not match local full-text search.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='search_fts' AND type='table'").fetchone():
        return  # SQLite builds without FTS5 retain history and use LIKE reads.
    conn.execute("DELETE FROM search_fts WHERE kind='source_chunk' AND ref_id IN "
                 '(SELECT CAST(id AS TEXT) FROM source_chunks WHERE source_id=? AND stale=1)',
                 (source_id,))
    conn.execute("DELETE FROM search_fts WHERE kind='evidence' AND ref_id IN "
                 '(SELECT CAST(id AS TEXT) FROM evidence WHERE source_id=? AND stale=1)',
                 (source_id,))


def _supersede_note(conn, snapshot_id: int) -> str:
    row = conn.execute('SELECT sha256 FROM source_snapshots WHERE id=?', (snapshot_id,)).fetchone()
    digest = (row['sha256'] if row else '')[:12]
    return f'重扫后该标题已不在材料中；快照 {snapshot_id}（sha256 {digest}）'


def _supersede_absent_candidates(conn, source_id: int, headings, snapshot_id: int) -> int:
    """P2-4/N1: heading-derived pending candidates whose heading vanished are superseded.

    Only candidates carrying a snapshot_id are derived from the scanned heading
    set; assist_wiki concept candidates keep snapshot_id NULL and must survive a
    rescan that happens to drop one of its headings. knowledge_candidates.
    superseded_by is a FK to knowledge_candidates(id), so an integer snapshot id
    cannot be stored while PRAGMA foreign_keys is ON: self reference marks
    "superseded without a replacement candidate" and the snapshot id/digest stay
    in the review row note.
    """
    present = {normalize_candidate({'name': heading, 'gist': heading}, source='')['name']
               for heading in headings if heading}
    rows = conn.execute(
        "SELECT id,name FROM knowledge_candidates WHERE source_id=? AND status='pending'"
        " AND superseded_by IS NULL AND snapshot_id IS NOT NULL", (source_id,)).fetchall()
    rows = [row for row in rows if row['name'] not in present]
    note = _supersede_note(conn, snapshot_id)
    for row in rows:
        conn.execute("UPDATE knowledge_candidates SET status='superseded',superseded_by=id "
                     'WHERE id=?', (row['id'],))
        conn.execute('''INSERT INTO candidate_reviews(candidate_id,action,note,superseded_at)
                        VALUES(?, 'superseded', ?, datetime('now','localtime'))''',
                     (row['id'], note))
    return len(rows)


def _upsert_scan_candidates(conn, source_id: int, source_path: str, headings, snapshot_id: int) -> None:
    """022/P2-4: refresh pending candidates and supersede the ones that vanished."""
    for heading in dict.fromkeys(headings):
        if not heading:
            continue
        candidate = normalize_candidate({'name': heading, 'gist': heading}, source=source_path)
        existing = conn.execute(
            'SELECT id,status,superseded_by FROM knowledge_candidates WHERE source_id=? AND name=?',
            (source_id, candidate['name']),
        ).fetchone()
        if existing and existing['status'] == 'superseded':
            # A returning heading revives the retained row: UNIQUE(source_id,name)
            # forbids inserting a fresh same-name row while history is kept.
            conn.execute('''UPDATE knowledge_candidates
                            SET schema_version=?,kp_type=?,chapter=?,gist=?,source=?,confidence=?,
                                snapshot_id=?,status='pending',superseded_by=NULL
                            WHERE id=?''',
                         (candidate['schema_version'], candidate['kp_type'], candidate['chapter'],
                          candidate['gist'], candidate['source'], candidate['confidence'],
                          snapshot_id, existing['id']))
            continue
        if existing and existing['status'] != 'pending':
            # Confirmed/duplicate/merged/rejected candidates and their review
            # history are left untouched.
            continue
        if existing:
            conn.execute('''UPDATE knowledge_candidates
                            SET schema_version=?,kp_type=?,chapter=?,gist=?,source=?,confidence=?,
                                snapshot_id=?,status=?,superseded_by=NULL
                            WHERE id=?''',
                         (candidate['schema_version'], candidate['kp_type'], candidate['chapter'],
                          candidate['gist'], candidate['source'], candidate['confidence'],
                          snapshot_id, candidate['status'], existing['id']))
            continue
        conn.execute('''INSERT INTO knowledge_candidates
                        (source_id,schema_version,name,kp_type,chapter,gist,source,confidence,status,snapshot_id)
                        VALUES(?,?,?,?,?,?,?,?,?,?)''',
                     (source_id, candidate['schema_version'], candidate['name'], candidate['kp_type'],
                      candidate['chapter'], candidate['gist'], candidate['source'],
                      candidate['confidence'], candidate['status'], snapshot_id))
    _supersede_absent_candidates(conn, source_id, headings, snapshot_id)


def _snapshot_paths(course_dir: Path, source_path: Path, digest: str) -> tuple[Path, Path]:
    version_dir = course_dir / 'source' / 'versions'
    version_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r'[^A-Za-z0-9_.\-\u4e00-\u9fff]+', '_', source_path.stem)[:100] or 'material'
    return version_dir / f'{stem}-{digest}{source_path.suffix.lower()}', version_dir / f'{stem}-{digest}.txt'


def _write_snapshot(course_dir: Path, source_path: Path, digest: str, content: str,
                    *, managed: bool) -> tuple[str, str] | None:
    # Only inbox material is copied into the course's managed history. An
    # externally linked folder remains user-owned and is never written into.
    if not managed:
        return None
    raw_path, text_path = _snapshot_paths(course_dir, source_path, digest)
    if not raw_path.exists():
        copy2(source_path, raw_path)
    if not text_path.exists():
        text_path.write_text(content, encoding='utf-8')
    return str(raw_path), str(text_path)


@_serialized_scan
def scan_course(course: str, folders: list[Path], *, force_ocr: bool = False,
                only_path: Path | None = None, expected_digest: str | None = None) -> dict:
    conn = connect()
    result = {'indexed': 0, 'unchanged': 0, 'failed': 0, 'stale': 0, 'truncated': 0, 'files': []}
    try:
        row = conn.execute('SELECT id FROM courses WHERE name=?', (course,)).fetchone()
        if not row:
            raise ValueError('课程不存在')
        cid = row['id']
        for folder in folders:
            if not folder.is_dir():
                continue
            seen_paths: set[str] = set()
            for path in ([only_path] if only_path else sorted(folder.rglob('*'))):
                checkpoint()
                if not path.is_file() or path.is_symlink() or path.name.startswith('.'):
                    continue
                if path.suffix.lower() not in SUPPORTED:
                    continue
                path = path.resolve()
                seen_paths.add(str(path))
                item = {'path': str(path)}
                try:
                    file_size = path.stat().st_size
                    if file_size > MAX_BYTES:
                        raise ValueError('文件超过 200 MB')
                    if path.suffix.lower() == '.pdf' and file_size > MAX_PDF_EXTRACTION_BYTES:
                        raise ExtractionRequiredError(
                            'PDF 已保存，但超过本机单次解析保护上限 100 MB；请拆分 PDF 或补充外部 OCR 后重试'
                        )
                    digest = file_digest(path)
                    if expected_digest and digest != expected_digest:
                        raise ValueError('材料内容已变化，请重新扫描')
                    old = conn.execute('SELECT * FROM source_files WHERE course_id=? AND path=?', (cid, str(path))).fetchone()
                    same_digest = bool(old and old['sha256'] == digest)
                    current_evidence = None
                    if same_digest and old['status'] == 'indexed':
                        # A matching file hash alone does not validate legacy
                        # NULL snapshot evidence. Re-extract it from the file.
                        current_evidence = conn.execute('''SELECT 1 FROM evidence e
                            JOIN source_snapshots ss ON ss.id=e.snapshot_id AND ss.source_id=e.source_id
                            WHERE e.source_id=? AND e.stale=0 AND ss.sha256=? AND TRIM(e.content)!=''
                              AND NOT EXISTS (SELECT 1 FROM evidence other
                                  WHERE other.source_id=e.source_id AND other.stale=0
                                    AND (other.snapshot_id IS NULL OR other.snapshot_id!=ss.id))
                              AND EXISTS (SELECT 1 FROM source_chunks sc
                                  WHERE sc.source_id=e.source_id AND sc.stale=0 AND sc.snapshot_id=ss.id)
                              AND NOT EXISTS (SELECT 1 FROM source_chunks sc
                                  WHERE sc.source_id=e.source_id AND sc.stale=0
                                    AND (sc.snapshot_id IS NULL OR sc.snapshot_id!=ss.id))
                            LIMIT 1''', (old['id'], digest)).fetchone()
                    if current_evidence:
                        # Backfill snapshots for files indexed before version
                        # history was introduced, without rebuilding chunks or candidates.
                        version = conn.execute(
                            'SELECT id FROM source_versions WHERE source_id=? AND sha256=?',
                            (old['id'], digest)
                        ).fetchone()
                        artifact = conn.execute(
                            'SELECT 1 FROM source_version_artifacts WHERE source_id=? AND sha256=?',
                            (old['id'], digest)
                        ).fetchone()
                        if not artifact and folder.name == 'inbox':
                            try:
                                content, _, _ = extract_structured(path)
                                snapshot = _write_snapshot(folder.parent, path, digest, content, managed=True)
                                if version and snapshot:
                                    with conn:
                                        conn.execute('''INSERT OR REPLACE INTO source_version_artifacts
                                            (source_version_id,source_id,sha256,original_path,raw_path,text_path)
                                            VALUES(?,?,?,?,?,?)''',
                                            (version['id'], old['id'], digest, str(path), snapshot[0], snapshot[1]))
                            except IngestCancelled:
                                raise
                            except Exception:
                                pass
                        result['unchanged'] += 1
                        continue
                    if old and old['sha256'] == digest and old['status'] == 'ocr_required' and not force_ocr:
                        result['unchanged'] += 1
                        item.update(status='ocr_required', error=old['error'])
                        result['files'].append(item)
                        continue
                    content, headings, evidence = extract_structured(path)
                    if (expected_digest or same_digest) and file_digest(path) != digest:
                        raise ValueError('识别期间材料内容已变化，请重新扫描')
                    checkpoint('indexing')
                    if not content:
                        raise ValueError('未提取到文字；扫描版 PDF 需要 OCR')
                    if same_digest and not any(item['text'].strip() for item in evidence):
                        raise ValueError('未提取到可核验的证据，请重新扫描')
                    course_dir = folder.parent if folder.name == 'inbox' else folder
                    repair_only = False
                    if same_digest:
                        # Existing derived content identifies a legacy repair,
                        # including retries after an extraction error. A failed
                        # first import with no derivatives must complete normally.
                        derived = conn.execute('''SELECT 1 FROM knowledge_candidates WHERE source_id=?
                            UNION ALL SELECT 1 FROM graph_candidates WHERE source_id=?
                            UNION ALL SELECT 1 FROM knowledge_notes WHERE source_id=? LIMIT 1''',
                            (old['id'], old['id'], old['id'])).fetchone()
                        stem = wiki._slug(str(path))
                        repair_only = bool(derived or any(
                            (course_dir / directory / (stem + suffix)).exists()
                            for directory, suffix in (('notes', '.md'), ('dictionary', '.json'))))
                    if not repair_only and folder.name == 'inbox' and folder.parent.name:
                        source_dir = folder.parent / 'source'
                        source_dir.mkdir(parents=True, exist_ok=True)
                        copy2(path, source_dir / path.name)
                    with conn:
                        conn.execute('''INSERT INTO source_files(course_id,path,sha256,status,error,indexed_at)
                                        VALUES(?,?,?,'indexed','',?)
                                        ON CONFLICT(course_id,path) DO UPDATE SET sha256=excluded.sha256,
                                        status='indexed', error='', indexed_at=excluded.indexed_at''',
                                     (cid, str(path), digest, datetime.now().isoformat(timespec='seconds')))
                        sid = conn.execute('SELECT id FROM source_files WHERE course_id=? AND path=?', (cid, str(path))).fetchone()['id']
                        conn.execute('''INSERT OR IGNORE INTO source_versions(source_id,sha256,status)
                                        VALUES(?,?, 'indexed')''', (sid, digest))
                        version = conn.execute(
                            'SELECT id FROM source_versions WHERE source_id=? AND sha256=?', (sid, digest)
                        ).fetchone()
                        snapshot = _write_snapshot(course_dir, path, digest, content, managed=folder.name == 'inbox')
                        if snapshot and version:
                            conn.execute('''INSERT OR REPLACE INTO source_version_artifacts
                                (source_version_id,source_id,sha256,original_path,raw_path,text_path)
                                VALUES(?,?,?,?,?,?)''',
                                (version['id'], sid, digest, str(path), snapshot[0], snapshot[1]))
                        # 022: keep history. A rescan retires the previous
                        # extraction as stale rows instead of deleting chunks,
                        # evidence and candidates (which cascaded into
                        # candidate_reviews and kp_evidence). Identical evidence
                        # content is reused so existing links stay valid.
                        snapshot_id = _source_snapshot(conn, sid, digest)
                        _retire_stale_source_rows(conn, sid)
                        if not repair_only:
                            conn.execute('DELETE FROM graph_candidates WHERE source_id=?', (sid,))
                        # source_chunks has UNIQUE(source_id,position), so the
                        # new generation continues after the retained rows.
                        first_position = conn.execute(
                            'SELECT COALESCE(MAX(position),-1)+1 AS position FROM source_chunks WHERE source_id=?',
                            (sid,)).fetchone()['position']
                        chunks, chunks_truncated = _chunks(content)
                        if chunks_truncated:
                            # Explicit truncation marker on the scan result (the
                            # response surfaces it); evidence rows stay complete.
                            item['truncated'] = True
                            item['truncated_reason'] = 'chunk_limit'
                            result['truncated'] = result.get('truncated', 0) + 1
                        item['chunk_count'] = len(chunks)
                        conn.executemany(
                            'INSERT INTO source_chunks(source_id,position,content,snapshot_id,stale) '
                            'VALUES(?,?,?,?,0)',
                            [(sid, first_position + i, chunk, snapshot_id) for i, chunk in enumerate(chunks)])
                        conn.executemany('''INSERT INTO evidence
                            (source_id,chunk_id,locator_type,locator_start,locator_end,content,content_hash,
                             snapshot_id,stale)
                            VALUES(?,?,?,?,?,?,?,?,0)
                            ON CONFLICT(source_id,locator_type,locator_start,locator_end,content_hash)
                            DO UPDATE SET stale=0, content=excluded.content, snapshot_id=excluded.snapshot_id''', [
                                (sid, None, item['locator_type'], item['locator_start'], item['locator_end'],
                                 item['text'], hashlib.sha256(item['text'].encode('utf-8')).hexdigest(),
                                 snapshot_id)
                                for item in evidence if item['text']
                            ])
                        if not repair_only:
                            _upsert_scan_candidates(conn, sid, str(path), headings, snapshot_id)
                    artifacts = {}
                    if not repair_only:
                        # Same-hash repair upgrades only the verified index;
                        # notes/dictionaries and their rows may be hand edited.
                        artifacts = wiki.write_artifacts(
                            course_dir=course_dir, course=course, source_path=str(path),
                            source_sha256=digest, content=content, headings=headings,
                            evidence=evidence, domain=domain_profiles.profile_for_course(course)['id'],
                        )
                        with conn:
                            conn.execute('''INSERT OR REPLACE INTO knowledge_notes
                                (source_id,path,source_sha256,status,updated_at)
                                VALUES(?,?,?,'draft',?)''',
                                (sid, artifacts['note'], digest, datetime.now().isoformat(timespec='seconds')))
                    item['status'] = 'indexed'
                    item['artifacts'] = artifacts
                    result['indexed'] += 1
                except IngestCancelled:
                    raise
                except Exception as exc:
                    with conn:
                        failure_status = 'ocr_required' if isinstance(exc, ExtractionRequiredError) else 'error'
                        existing = conn.execute('SELECT id FROM source_files WHERE course_id=? AND path=?',
                                                (cid, str(path))).fetchone()
                        if failure_status == 'ocr_required' and existing:
                            # 022: material waiting for OCR keeps prior
                            # extraction as stale history instead of losing it.
                            _retire_stale_source_rows(conn, existing['id'])
                        conn.execute(f'''INSERT INTO source_files(course_id,path,status,error) VALUES(?,?,'{failure_status}',?)
                                        ON CONFLICT(course_id,path) DO UPDATE SET status='error',error=excluded.error''',
                                     (cid, str(path), str(exc)[:300]))
                    if failure_status == 'ocr_required':
                        with conn:
                            conn.execute("UPDATE source_files SET status='ocr_required' WHERE course_id=? AND path=?",
                                         (cid, str(path)))
                    item.update(status=failure_status, error=str(exc)[:300])
                    result['failed'] += 1
                result['files'].append(item)
            if folder.name == 'inbox' and only_path is None:
                prefix = str(folder.resolve()) + '/'
                stale_rows = conn.execute(
                    "SELECT id,path FROM source_files WHERE course_id=? AND path LIKE ? AND status='indexed'",
                    (cid, prefix + '%'),
                ).fetchall()
                for stale in stale_rows:
                    if stale['path'] not in seen_paths:
                        conn.execute("UPDATE source_files SET status='stale', error='原文件已移出课程投放目录' WHERE id=?",
                                     (stale['id'],))
                        result['stale'] += 1
                conn.commit()
        return result
    finally:
        conn.close()


def search(query: str, course: str | None = None, limit: int = 6,
           workspace: str | None = None, *, include_dev: bool = False) -> list[dict]:
    """Material chunk search; development docs are hidden unless include_dev=True."""
    from .repo import keywords_of
    terms = [term for term in keywords_of(query) if len(term) >= 2][:8]
    if not terms:
        terms = [query.strip()]
    conn = connect()
    try:
        dev_filter = '' if include_dev else " AND sf.source_kind!='development_doc'"
        sql = '''SELECT sc.id, sc.content, sf.path, co.name AS course_name FROM source_chunks sc
                 JOIN source_files sf ON sf.id=sc.source_id JOIN courses co ON co.id=sf.course_id
                 WHERE sf.status='indexed' AND sc.stale=0''' + dev_filter + ''' AND (''' + ' OR '.join('sc.content LIKE ?' for _ in terms) + ')'
        args: list = [f'%{term}%' for term in terms]
        if course:
            sql += ' AND co.name=?'
            args.append(course)
        if workspace:
            sql += " AND COALESCE((SELECT key FROM workspaces WHERE id=co.workspace_id),'studypal')=?"
            args.append(workspace)
        sql += ' ORDER BY sc.id DESC LIMIT ?'
        args.append(max(1, min(limit, 30)))
        hits = []
        for row in conn.execute(sql, args):
            content = row['content'] or ''
            hits.append({'id': row['id'], 'name': Path(row['path']).name,
                         'gist': content[:CHUNK_SIZE], 'detail': '',
                         'source': row['path'], 'course_name': row['course_name'],
                         'chapter': '课程文件', 'mastery': '', 'kind': 'file',
                         'ref_kind': 'source_chunk', 'ref_id': row['id'],
                         'truncated': len(content) > CHUNK_SIZE})
        return hits
    finally:
        conn.close()


def assist_wiki(course: str, source_path: str, *, tier: str = 'fast', chapter: str = '') -> dict:
    """Run the optional model pass for one indexed file and keep it review-only."""
    requested = Path(source_path).expanduser().resolve()
    conn = connect()
    try:
        source = conn.execute('''SELECT sf.*, co.name AS course_name,
                                        COALESCE(ws.kind,'study') AS workspace_kind,
                                        COALESCE(ws.domain,'') AS workspace_domain
                                 FROM source_files sf JOIN courses co ON co.id=sf.course_id
                                 LEFT JOIN workspaces ws ON ws.id=co.workspace_id
                                 WHERE co.name=? AND sf.path=?''', (course, str(requested))).fetchone()
        if not source:
            raise ValueError('课程中没有找到这份已索引材料')
        if source['status'] != 'indexed':
            raise ValueError('只有已索引材料才能进行辅助拆录')
        path = Path(source['path'])
        if not path.is_file():
            raise ValueError('原材料文件已不存在，请先恢复文件并重新扫描')
        if file_digest(path) != source['sha256']:
            raise ValueError('材料内容已变化，请先重新扫描')
        content, headings, evidence = extract_structured(path)
        if file_digest(path) != source['sha256']:
            raise ValueError('识别期间材料内容已变化，请先重新扫描')
        evidence_rows = conn.execute(
            'SELECT id,locator_type,locator_start,locator_end,content FROM evidence '
            'WHERE source_id=? AND stale=0 ORDER BY id', (source['id'],)
        ).fetchall()
        evidence_ids_by_location = {}
        for row in evidence_rows:
            key = (row['locator_type'], row['locator_start'], row['locator_end'], row['content'])
            evidence_ids_by_location.setdefault(key, row['id'])
        # Validate the entire extraction before chapter filtering or artifact writes.
        if any(item.get('text') and
               (item.get('locator_type'), item.get('locator_start'), item.get('locator_end'), item['text'])
               not in evidence_ids_by_location for item in evidence):
            raise ValueError('数据库中的证据与当前文件不一致，请先重新扫描')
        workspace = {'study': 'StudyPal', 'work': 'WorkPal', 'life': 'LifePal'}.get(
            source['workspace_kind'].strip().casefold(), '')
        profile = domain_profiles.profile_for_course(course, workspace or 'general')
        explicit_profile = domain_profiles.load_profiles().get(source['workspace_domain'].strip().casefold())
        if explicit_profile and explicit_profile['workspace'].strip().casefold() in {'', workspace.casefold()}:
            profile = explicit_profile
        selected_chapter = ''
        if chapter:
            section = next((item for item in chapter_sections(evidence)
                            if item['chapter'] == chapter or item['chapter'].startswith(chapter)), None)
            if not section:
                raise ValueError(f'材料中没有找到章节：{chapter}')
            content, evidence, selected_chapter = section['content'], section['evidence'], section['chapter']
            headings = [selected_chapter]
        evidence_ids_by_ref = [evidence_ids_by_location.get(
            (item.get('locator_type'), item.get('locator_start'), item.get('locator_end'), item.get('text'))
        ) for item in evidence]
        result = wiki.assist_draft(
            course_dir=(path.parent.parent if path.parent.name == 'inbox' else path.parent),
            course=course, source_path=source['path'], source_sha256=source['sha256'],
            content=content, headings=headings, evidence=evidence, tier=tier,
            domain=profile['id'], chapter=selected_chapter,
            artifact_suffix=selected_chapter,
        )
        with conn:
            for item in result['concepts']:
                candidate = normalize_candidate(item, source=source['path'])
                # Chapter-scoped extraction must inherit the canonical chapter
                # selected by the user; model prose cannot create a new tree.
                if selected_chapter:
                    candidate['chapter'] = selected_chapter
                candidate['status'] = 'pending'
                candidate['evidence_refs'] = list(item.get('evidence_refs', []))
                existing = conn.execute(
                    '''SELECT kc.id,kc.status,EXISTS(
                           SELECT 1 FROM candidate_reviews cr WHERE cr.candidate_id=kc.id
                           AND cr.action!='superseded') AS reviewed
                       FROM knowledge_candidates kc WHERE kc.source_id=? AND kc.name=?''',
                    (source['id'], candidate['name']),
                ).fetchone()
                if existing and (existing['status'] not in {'pending', 'superseded'} or existing['reviewed']):
                    continue
                if existing:
                    conn.execute('''UPDATE knowledge_candidates SET schema_version=?,kp_type=?,chapter=?,
                                    gist=?,detail=?,trap=?,source=?,confidence=?,status='pending',
                                    superseded_by=NULL WHERE id=?''',
                                 (candidate['schema_version'], candidate['kp_type'], candidate['chapter'],
                                  candidate['gist'], candidate['detail'], candidate['trap'], candidate['source'],
                                  candidate['confidence'], existing['id']))
                    candidate_id = existing['id']
                else:
                    cur = conn.execute('''INSERT INTO knowledge_candidates
                        (source_id,schema_version,name,kp_type,chapter,gist,detail,trap,source,confidence,status)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                        (source['id'], candidate['schema_version'], candidate['name'], candidate['kp_type'],
                         candidate['chapter'], candidate['gist'], candidate['detail'], candidate['trap'],
                         candidate['source'], candidate['confidence'], 'pending'))
                    candidate_id = cur.lastrowid
                conn.execute('DELETE FROM knowledge_candidate_evidence WHERE candidate_id=?', (candidate_id,))
                refs = candidate.get('evidence_refs', [])
                evidence_ids = []
                for ref in refs:
                    if type(ref) is not int or not 1 <= ref <= len(evidence_ids_by_ref):
                        continue
                    evidence_id = evidence_ids_by_ref[ref - 1]
                    if evidence_id is not None:
                        evidence_ids.append(evidence_id)
                conn.executemany(
                    'INSERT OR IGNORE INTO knowledge_candidate_evidence(candidate_id,evidence_id) VALUES(?,?)',
                    [(candidate_id, evidence_id) for evidence_id in dict.fromkeys(evidence_ids)]
                )
            conn.execute('''UPDATE knowledge_notes SET status='assisted_draft',updated_at=?
                            WHERE source_id=? AND source_sha256=?''',
                         (datetime.now().isoformat(timespec='seconds'), source['id'], source['sha256']))
        result['course'] = course
        result['source'] = source['path']
        result['source_id'] = source['id']
        return result
    finally:
        conn.close()


def delete_assisted_draft(course: str, source_path: str) -> dict:
    """Remove generated Wiki/dictionary drafts while preserving source evidence."""
    requested = Path(source_path).expanduser().resolve()
    conn = connect()
    deleted: list[str] = []
    try:
        source = conn.execute('''SELECT sf.*, co.name AS course_name
                                 FROM source_files sf JOIN courses co ON co.id=sf.course_id
                                 WHERE co.name=? AND sf.path=?''', (course, str(requested))).fetchone()
        if not source:
            raise ValueError('课程中没有找到这份已索引材料')
        course_dir = requested.parent.parent if requested.parent.name == 'inbox' else requested.parent
        note_rows = conn.execute('''SELECT path FROM knowledge_notes
                                    WHERE source_id=? AND source_sha256=?''',
                                 (source['id'], source['sha256'])).fetchall()
        candidates = [Path(row['path']).expanduser().resolve() for row in note_rows if row['path']]
        candidates.append(course_dir / 'dictionary' / f'{wiki._slug(str(requested))}.json')
        for target in dict.fromkeys(candidates):
            try:
                target.relative_to(course_dir)
            except ValueError as exc:
                raise ValueError('拆录产物路径不在课程目录内，已停止删除') from exc
            if target.is_file():
                target.unlink()
                deleted.append(str(target))
        with conn:
            conn.execute('''UPDATE knowledge_notes SET status='draft',updated_at=?
                            WHERE source_id=? AND source_sha256=?''',
                         (datetime.now().isoformat(timespec='seconds'), source['id'], source['sha256']))
        return {'ok': True, 'course': course, 'source': str(requested), 'deleted': deleted,
                'status': 'draft'}
    finally:
        conn.close()


def _version_artifact(conn, source_id: int, sha256: str):
    return conn.execute('''SELECT sva.* FROM source_version_artifacts sva
                           WHERE sva.source_id=? AND sva.sha256=?''', (source_id, sha256)).fetchone()


def diff_versions(course: str, source_path: str, from_sha: str, to_sha: str = 'latest') -> dict:
    requested = str(Path(source_path).expanduser().resolve())
    conn = connect()
    try:
        source = conn.execute('''SELECT sf.* FROM source_files sf JOIN courses co ON co.id=sf.course_id
                                 WHERE co.name=? AND sf.path=?''', (course, requested)).fetchone()
        if not source:
            raise ValueError('课程中没有找到这份材料')
        target_sha = source['sha256'] if to_sha == 'latest' else to_sha
        old = _version_artifact(conn, source['id'], from_sha)
        new = _version_artifact(conn, source['id'], target_sha)
        if not old or not new:
            raise ValueError('指定版本没有可用的本地快照')
        old_text = Path(old['text_path']).read_text(encoding='utf-8')
        new_text = Path(new['text_path']).read_text(encoding='utf-8')
        diff = '\n'.join(difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(),
            fromfile=f'{Path(requested).name}@{from_sha[:12]}',
            tofile=f'{Path(requested).name}@{target_sha[:12]}', lineterm=''
        ))
        return {'course': course, 'source': requested, 'from_sha': from_sha,
                'to_sha': target_sha, 'changed': old_text != new_text, 'diff': diff}
    finally:
        conn.close()


def restore_version(course: str, source_path: str, sha256: str) -> dict:
    requested = Path(source_path).expanduser().resolve()
    conn = connect()
    try:
        source = conn.execute('''SELECT sf.* FROM source_files sf JOIN courses co ON co.id=sf.course_id
                                 WHERE co.name=? AND sf.path=?''', (course, str(requested))).fetchone()
        if not source:
            raise ValueError('课程中没有找到这份材料')
        artifact = _version_artifact(conn, source['id'], sha256)
        if not artifact or not Path(artifact['raw_path']).is_file():
            raise ValueError('指定版本没有可恢复的原始文件快照')
        requested.parent.mkdir(parents=True, exist_ok=True)
        copy2(artifact['raw_path'], requested)
    finally:
        conn.close()
    result = scan_course(course, [requested.parent])
    item = next((row for row in result['files'] if row.get('path') == str(requested)), None)
    if not item or item.get('status') != 'indexed':
        raise ValueError('历史文件已恢复，但重新索引失败')
    return {'ok': True, 'course': course, 'source': str(requested), 'sha256': sha256,
            'status': item['status'], 'index': item}


def versions(course: str, source_path: str) -> list[dict]:
    requested = str(Path(source_path).expanduser().resolve())
    conn = connect()
    try:
        source = conn.execute('''SELECT sf.id FROM source_files sf JOIN courses co ON co.id=sf.course_id
                                 WHERE co.name=? AND sf.path=?''', (course, requested)).fetchone()
        if not source:
            raise ValueError('课程中没有找到这份材料')
        rows = conn.execute('''SELECT sv.sha256,sv.status,sv.captured_at,
                                      sva.raw_path,sva.text_path
                               FROM source_versions sv LEFT JOIN source_version_artifacts sva
                               ON sva.source_version_id=sv.id
                               WHERE sv.source_id=? ORDER BY sv.id DESC''', (source['id'],)).fetchall()
        return [{
            'sha256': row['sha256'], 'status': row['status'], 'captured_at': row['captured_at'],
            'has_raw': bool(row['raw_path'] and Path(row['raw_path']).is_file()),
            'has_text': bool(row['text_path'] and Path(row['text_path']).is_file()),
        } for row in rows]
    finally:
        conn.close()


def status(course: str) -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute('''SELECT sf.id,sf.path,sf.sha256,sf.status,sf.error,sf.indexed_at,
                 (SELECT COUNT(*) FROM source_chunks WHERE source_id=sf.id AND stale=0) AS chunks
                 ,(SELECT COUNT(*) FROM source_versions WHERE source_id=sf.id) AS versions
                 ,sf.source_kind,sf.source_role,sf.catalog_status,sf.ingest_run_id
                 FROM source_files sf JOIN courses co ON co.id=sf.course_id
                 WHERE co.name=? ORDER BY sf.path''', (course,))]
    finally:
        conn.close()


def inventory(course: str) -> list[dict]:
    """Return user-facing material and generated artifact paths for a course."""
    conn = connect()
    try:
        rows = conn.execute('''SELECT sf.id,sf.path,sf.sha256,sf.status,sf.error,sf.indexed_at,
                 (SELECT COUNT(*) FROM source_chunks WHERE source_id=sf.id AND stale=0) AS chunks,
                 (SELECT COUNT(*) FROM source_versions WHERE source_id=sf.id) AS versions,
                 sf.source_kind,sf.source_role,sf.catalog_status,sf.ingest_run_id,
                 (SELECT path FROM knowledge_notes WHERE source_id=sf.id ORDER BY id DESC LIMIT 1) AS note_path,
                 (SELECT status FROM knowledge_notes WHERE source_id=sf.id ORDER BY id DESC LIMIT 1) AS note_status
                 FROM source_files sf JOIN courses co ON co.id=sf.course_id
                 WHERE co.name=? ORDER BY sf.path''', (course,)).fetchall()
        result = []
        for row in rows:
            source_path = Path(row['path']).resolve()
            course_dir = source_path.parent.parent if source_path.parent.name == 'inbox' else source_path.parent
            source_copy = course_dir / 'source' / source_path.name
            note_path = Path(row['note_path']).resolve() if row['note_path'] else None
            dictionary_path = course_dir / 'dictionary' / f'{wiki._slug(str(source_path))}.json'
            artifact = conn.execute('''SELECT raw_path,text_path FROM source_version_artifacts
                                      WHERE source_id=? ORDER BY id DESC LIMIT 1''', (row['id'],)).fetchone()
            raw_path = Path(artifact['raw_path']).resolve() if artifact and artifact['raw_path'] else None
            text_path = Path(artifact['text_path']).resolve() if artifact and artifact['text_path'] else None

            def entry(label: str, path: Path | None) -> dict:
                return {'label': label, 'path': str(path) if path else '',
                        'exists': bool(path and path.is_file())}

            try:
                relative = str(source_path.relative_to(course_dir))
            except ValueError:
                relative = source_path.name
            result.append({
                'source_id': row['id'], 'name': source_path.name,
                'relative_path': relative, 'original_path': str(source_path),
                'status': row['status'], 'error': row['error'],
                'indexed_at': row['indexed_at'], 'chunks': row['chunks'],
                'versions': row['versions'], 'note_status': row['note_status'] or 'draft',
                'artifacts': {
                    'source_copy': entry('课程原材料副本', source_copy),
                    'raw_snapshot': entry('原始版本快照', raw_path),
                    'text_snapshot': entry('文本版本快照', text_path),
                    'note': entry('Wiki 拆录', note_path),
                    'dictionary': entry('结构化词典', dictionary_path),
                },
            })
        return result
    finally:
        conn.close()


def candidates(course: str) -> list[dict]:
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute('''SELECT kc.id,kc.name AS label,kc.status,kc.schema_version,
                 kc.kp_type,kc.chapter,kc.gist,kc.confidence,kc.source FROM knowledge_candidates kc
                 JOIN source_files sf ON sf.id=kc.source_id JOIN courses co ON co.id=sf.course_id
                 WHERE co.name=? AND kc.superseded_by IS NULL AND kc.status<>'superseded'
                 ORDER BY CASE WHEN kc.snapshot_id IS NULL THEN 0
                               WHEN kc.snapshot_id=(SELECT MAX(ss.id) FROM source_snapshots ss
                                                    WHERE ss.source_id=kc.source_id AND ss.sha256=sf.sha256) THEN 0
                               ELSE 1 END, kc.id''', (course,))]
        for row in rows:
            row['evidence_ids'] = [item['evidence_id'] for item in conn.execute(
                'SELECT evidence_id FROM knowledge_candidate_evidence WHERE candidate_id=? ORDER BY evidence_id',
                (row['id'],)
            )]
        return rows
    finally:
        conn.close()


def confirm_candidate(candidate_id: int) -> dict:
    conn = connect()
    try:
        row = conn.execute('''SELECT kc.id,kc.source_id,kc.name,kc.kp_type,kc.chapter,kc.gist,kc.detail,kc.trap,
             kc.source,kc.status,kc.superseded_by,sf.course_id,co.name AS course_name FROM knowledge_candidates kc
             JOIN source_files sf ON sf.id=kc.source_id JOIN courses co ON co.id=sf.course_id
             WHERE kc.id=?''', (candidate_id,)).fetchone()
        if not row:
            raise ValueError('图谱候选不存在')
        if row['superseded_by'] is not None or row['status'] == 'superseded':
            raise ValueError('该图谱候选已因材料更新失效，请重新扫描后再审核')
        if row['status'] != 'pending':
            raise ValueError('该图谱候选已经审核')
        existing = conn.execute(
            "SELECT id,COALESCE(deleted_at,'') AS deleted_at FROM kp WHERE course_id=? AND name=?",
            (row['course_id'], row['name'])
        ).fetchone()
        with conn:
            if existing:
                if existing['deleted_at']:
                    # P3: re-confirming a name whose kp was soft-deleted revives
                    # the retained row (the UNIQUE(course_id,name) constraint
                    # would otherwise reject a fresh insert forever).
                    conn.execute("UPDATE kp SET deleted_at='' WHERE id=?", (existing['id'],))
                conn.execute("UPDATE knowledge_candidates SET status='duplicate' WHERE id=?", (candidate_id,))
                conn.execute('''INSERT INTO candidate_reviews(candidate_id,action,target_kp_id,note)
                                VALUES(?,?,?,?)''', (candidate_id, 'duplicate', existing['id'], '正式图谱已有同名节点'))
                return {'ok': True, 'status': 'duplicate', 'target_kp_id': existing['id'],
                        'label': row['name'], 'source': row['source']}
            cur = conn.execute('''INSERT INTO kp(course_id,name,source,chapter,gist,detail,trap,kp_type,band)
                                 VALUES(?,?,?,?,?,?,?,?, '文件')''',
                               (row['course_id'], row['name'], row['source'], row['chapter'], row['gist'],
                                row['detail'], row['trap'], row['kp_type']))
            target_id = cur.lastrowid
            domain = domain_profiles.profile_for_course(row['course_name'])['id']
            # P2-3/P3-a: retired (stale) evidence is history, never current
            # support. Only live evidence can back a newly confirmed kp: the
            # candidate's live links, then the source's live evidence; if both
            # are empty the kp is created without evidence refs.
            evidence_rows = conn.execute(
                '''SELECT e.id FROM evidence e JOIN knowledge_candidate_evidence kce ON kce.evidence_id=e.id
                   WHERE kce.candidate_id=? AND COALESCE(e.stale,0)=0 ORDER BY e.id LIMIT 20''',
                (candidate_id,)
            ).fetchall()
            if not evidence_rows:
                evidence_rows = conn.execute(
                    'SELECT id FROM evidence WHERE source_id=? AND stale=0 ORDER BY id LIMIT 20',
                    (row['source_id'],)
                ).fetchall()
            evidence_refs = [{'evidence_id': item['id']} for item in evidence_rows]
            conn.execute('''INSERT INTO knowledge_node_meta(kp_id,schema_version,domain,source_refs_json,status)
                            VALUES(?,?,?,?, 'confirmed')''',
                         (target_id, 'cyberpal.knowledge.v1', domain, json.dumps(evidence_refs, ensure_ascii=False)))
            conn.executemany('INSERT OR IGNORE INTO kp_evidence(kp_id,evidence_id,role) VALUES(?,?,?)',
                             [(target_id, item['id'], 'supports') for item in evidence_rows])
            conn.execute("UPDATE knowledge_candidates SET status='confirmed' WHERE id=?", (candidate_id,))
            conn.execute('''INSERT INTO candidate_reviews(candidate_id,action,target_kp_id)
                            VALUES(?,?,?)''', (candidate_id, 'confirmed', target_id))
        return {'ok': True, 'status': 'confirmed', 'target_kp_id': target_id,
                'label': row['name'], 'source': row['source']}
    finally:
        conn.close()


def review_candidate(candidate_id: int, action: str, target_kp_id: int | None = None,
                     note: str = '') -> dict:
    if action not in {'rejected', 'merged'}:
        raise ValueError('候选审核动作只能是 rejected 或 merged')
    conn = connect()
    try:
        row = conn.execute(
            'SELECT id,status,superseded_by FROM knowledge_candidates WHERE id=?', (candidate_id,)
        ).fetchone()
        if not row:
            raise ValueError('图谱候选不存在')
        if row['superseded_by'] is not None or row['status'] == 'superseded':
            raise ValueError('该图谱候选已因材料更新失效，请重新扫描后再审核')
        if row['status'] != 'pending':
            raise ValueError('该图谱候选已经审核')
        if action == 'merged':
            if not target_kp_id or not conn.execute('SELECT 1 FROM kp WHERE id=?', (target_kp_id,)).fetchone():
                raise ValueError('合并候选必须指定有效的正式节点')
        with conn:
            conn.execute('UPDATE knowledge_candidates SET status=? WHERE id=?', (action, candidate_id))
            conn.execute('''INSERT INTO candidate_reviews(candidate_id,action,target_kp_id,note)
                            VALUES(?,?,?,?)''', (candidate_id, action, target_kp_id, note[:500]))
        return {'ok': True, 'status': action, 'target_kp_id': target_kp_id}
    finally:
        conn.close()


# ── T2.9 / O-DATA-7: single formal graph write channel ─────────────────
# Maintenance/import scripts must not write kp/kp_edges through the ordinary
# candidate -> confirm flow silently. They declare a whitelisted script name,
# get a channel descriptor, and append one audit row to ingest_runs (016, no new
# migration) after the write. 011's historical triggers are frozen, so retrieval
# filters development_doc rows at read time instead (search_index).
FORMAL_WRITE_SCRIPTS = {
    'import_wiki_workpal': 'scripts/import_wiki_workpal.py',
    'rebuild_graph_data': 'scripts/rebuild_graph_data.py',
    'import_graph': 'app/memory/graph_tools.py',
}
MAINTENANCE_SOURCE_KIND = 'maintenance_import'


def _caller_script_name() -> str:
    argv = getattr(sys, 'argv', None) or ['']
    first = str(argv[0] or '') if argv else ''
    return Path(first).name if first else ''


def _is_test_runner(caller: str) -> bool:
    # '' / '-c' / '-m' cover python -c and python -m; '__main__.py' is the
    # pytest/unittest module entry point; the plain names cover console runners.
    # Production maintenance scripts run as scripts/<name>.py.
    return (caller in {'', '-c', '-m', 'unittest', '__main__.py'}
            or 'pytest' in caller or caller.startswith('py.test'))


def authorize_formal_write(script: str, *, allow_api: bool = False) -> dict:
    """Validate a maintenance/import caller before it writes the formal graph.

    Only allow-listed scripts may use this channel; the caller file name must
    match the declared script (test runners are exempt so this stays testable).
    allow_api is reserved for the single UI import entry point (graph_tools),
    where the actor is a user action rather than a maintenance script.
    """
    key = Path(str(script or '').strip()).name
    if key.endswith('.py'):
        key = key[:-3]
    if key not in FORMAL_WRITE_SCRIPTS:
        raise ValueError(f'未授权的正式图写入调用方：{script}')
    caller = _caller_script_name()
    allowed = Path(FORMAL_WRITE_SCRIPTS[key]).name
    if not allow_api and not (_is_test_runner(caller) or caller == allowed):
        raise ValueError(f'正式图写入通道调用方不匹配：{caller}（声明 {key}）')
    return {'script': key, 'path': FORMAL_WRITE_SCRIPTS[key],
            'owner_only': True, 'allow_api': bool(allow_api)}


def record_formal_write(channel: dict, *, course_id: int | None = None,
                        nodes: int = 0, edges: int = 0, detail: str = '') -> int:
    """Append the audit row for one authorized formal graph write.

    The row lands in ingest_runs (source_kind='maintenance_import',
    source_role='owner_only', extractor='script:<name>') so imports remain
    traceable without a new table or migration. Returns the audit run id.
    """
    if not isinstance(channel, dict) or channel.get('script') not in FORMAL_WRITE_SCRIPTS:
        raise ValueError('正式图写入通道未授权，禁止写入')
    counts = max(0, int(nodes)) + max(0, int(edges))
    conn = connect()
    try:
        with conn:
            cursor = conn.execute(
                '''INSERT INTO ingest_runs
                   (course_id,source_kind,source_role,status,extractor,error,artifact_count,finished_at)
                   VALUES(?,?,'owner_only','completed',?,?,?,datetime('now','localtime'))''',
                (course_id, MAINTENANCE_SOURCE_KIND,
                 'script:' + str(channel['script']), str(detail or '')[:300], counts))
            return int(cursor.lastrowid)
    finally:
        conn.close()


def formal_write_audit(channel: dict) -> dict:
    """Last audit row for one channel; used by tests and maintenance reports."""
    if not isinstance(channel, dict) or channel.get('script') not in FORMAL_WRITE_SCRIPTS:
        raise ValueError('正式图写入通道未授权')
    conn = connect()
    try:
        row = conn.execute(
            '''SELECT * FROM ingest_runs
                WHERE source_kind=? AND extractor=?
                ORDER BY id DESC LIMIT 1''',
            (MAINTENANCE_SOURCE_KIND, 'script:' + str(channel['script']))).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()
