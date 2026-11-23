"""B3 T3.5 材料原文/分块读取：source 元数据、evidence 分块、页图与上下文。"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader, PdfWriter

from app.api import routes
from app.memory import db, materials


def _text_pdf_bytes(text='Chapter 1 Demand and marginal cost'):
    content = ('BT /F1 18 Tf 72 720 Td (' + text + ') Tj ET').encode('latin-1')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
        b'/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
        b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += str(number).encode() + b' 0 obj\n' + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 ' + str(len(objects) + 1).encode() + b'\n0000000000 65535 f \n'
    for offset in offsets:
        out += ('%010d 00000 n \n' % offset).encode()
    out += (b'trailer\n<< /Size ' + str(len(objects) + 1).encode() +
            b' /Root 1 0 R >>\nstartxref\n' + str(xref).encode() + b'\n%%EOF\n')
    return bytes(out)


def _write_text_pdf(path, *, pages=1, text='Chapter 1 Demand and marginal cost'):
    reader = PdfReader(io.BytesIO(_text_pdf_bytes(text)))
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_page(reader.pages[0])
    with path.open('wb') as handle:
        writer.write(handle)
    return path


def _write_blank_pdf(path, *, pages=2):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    with path.open('wb') as handle:
        writer.write(handle)
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    db_path = root / 'app' / 'banxue.db'
    monkeypatch.setattr(db, 'DATA_DIR', root)
    monkeypatch.setattr(db, 'APP_DIR', root / 'app')
    monkeypatch.setattr(db, 'DB_PATH', db_path)
    db.apply_schema()
    folder = root / 'materials'
    folder.mkdir(parents=True)
    lines = [f'第{i}行材料内容' for i in range(1, 7)]
    txt = folder / 'notes.txt'
    txt.write_text('\n'.join(lines), encoding='utf-8')
    long_txt = folder / 'long.txt'
    long_text = '长' * (materials.MAX_BLOCK_CHARS + 120)
    long_txt.write_text(long_text, encoding='utf-8')
    pdf_text = folder / 'textbook.pdf'
    _write_text_pdf(pdf_text, pages=2)
    pdf_blank = folder / 'blank.pdf'
    _write_blank_pdf(pdf_blank)
    pdf_scanned = folder / 'scanned.pdf'
    Image.new('RGB', (120, 60), 'white').save(pdf_scanned, format='PDF')
    outside = tmp_path / 'outside.txt'
    outside.write_text('不在数据目录', encoding='utf-8')
    vanished = folder / 'vanished.txt'

    conn = db.connect()
    try:
        course_id = conn.execute("INSERT INTO courses(name) VALUES('材料课')").lastrowid
        ids = {}

        def add_source(key, path, kind='material'):
            ids[key] = conn.execute(
                '''INSERT INTO source_files(course_id, path, sha256, status, source_kind)
                   VALUES(?,?,?,?,?)''',
                (course_id, str(path), hashlib.sha1(str(path).encode()).hexdigest(),
                 'indexed', kind)).lastrowid

        def add_evidence(source_id, locator_type, start, text, *, stale=0, snapshot_id=None):
            return conn.execute(
                '''INSERT INTO evidence(source_id, locator_type, locator_start, locator_end,
                                        content, content_hash, snapshot_id, stale)
                   VALUES(?,?,?,?,?,?,?,?)''',
                (source_id, locator_type, start, start, text,
                 hashlib.sha1(f'{source_id}:{locator_type}:{start}:{text}'.encode()).hexdigest(),
                 snapshot_id, stale)).lastrowid

        add_source('txt', txt)
        conn.executemany(
            "INSERT INTO source_chunks(source_id, position, content) VALUES(?,?,?)",
            [(ids['txt'], 0, 'chunk a'), (ids['txt'], 1, 'chunk b')])
        ids['txt_blocks'] = [
            add_evidence(ids['txt'], 'line', i, lines[i - 1]) for i in range(1, 7)
        ]
        ids['txt_stale'] = add_evidence(ids['txt'], 'line', 7, '历史快照里的旧块',
                                         stale=1)

        snap = folder / 'snap.txt'
        snap.write_text('快照行一\n快照行二\n快照行三\n', encoding='utf-8')
        add_source('snap', snap)
        snapshot_id = conn.execute(
            "INSERT INTO source_snapshots(source_id, sha256) VALUES(?,?)",
            (ids['snap'], 'snap-sha-1')).lastrowid
        ids['snap_old'] = add_evidence(ids['snap'], 'line', 1, '快照前旧块')
        ids['snap_stale'] = add_evidence(ids['snap'], 'line', 2, '快照里的过期块',
                                         stale=1, snapshot_id=snapshot_id)
        ids['snap_current'] = add_evidence(ids['snap'], 'line', 3, '快照里的当前块',
                                           snapshot_id=snapshot_id)

        add_source('long', long_txt)
        ids['long_block'] = add_evidence(ids['long'], 'line', 1, long_text)

        add_source('pdf_text', pdf_text, kind='textbook')
        ids['pdf_text_blocks'] = [
            add_evidence(ids['pdf_text'], 'page', page, f'Page {page} content')
            for page in (1, 2)
        ]

        add_source('pdf_blank', pdf_blank)
        add_evidence(ids['pdf_blank'], 'page', 1, ' ')

        add_source('pdf_scanned', pdf_scanned)
        add_evidence(ids['pdf_scanned'], 'page', 1, ' ')

        add_source('outside', outside)
        add_evidence(ids['outside'], 'line', 1, '外部文件')
        add_source('vanished', vanished)
        add_evidence(ids['vanished'], 'line', 1, '文件已经不存在')
        conn.commit()
    finally:
        conn.close()
    return {'root': root, 'outer': tmp_path, 'ids': ids, 'lines': lines,
            'txt': txt, 'pdf_text': pdf_text}


@pytest.fixture
def client(world, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    monkeypatch.setattr(routes.workflow_service, 'recover_stale_runs', lambda: 0,
                        raising=False)
    return TestClient(app)


def _json(response):
    return response.json()


def test_source_metadata(client, world):
    ids = world['ids']
    response = client.get(f'/api/materials/{ids["txt"]}')
    assert response.status_code == 200
    body = _json(response)
    source = body['source']
    assert source['id'] == ids['txt']
    assert source['path'] == str(world['txt'])
    assert source['source_kind'] == 'material'
    assert source['locator_types'] == ['line']
    assert source['chunk_count'] == 2
    assert source['evidence_count'] == 6
    assert body['total_evidence'] == 7
    assert 'total_evidence' not in source


def test_unknown_source_404(client):
    assert client.get('/api/materials/99999').status_code == 404
    assert client.get('/api/materials/99999/blocks').status_code == 404
    assert client.get('/api/materials/99999/page.png').status_code == 404


def test_blocks_pagination_and_continuation(client, world):
    source_id = world['ids']['txt']
    first = client.get(f'/api/materials/{source_id}/blocks', params={'limit': 2})
    assert first.status_code == 200
    body = _json(first)
    assert [block['locator_start'] for block in body['blocks']] == [1, 2]
    assert body['has_more'] is True
    assert body['blocks'][0]['text'] == world['lines'][0]
    assert body['blocks'][0]['truncated'] is False

    second = client.get(f'/api/materials/{source_id}/blocks',
                        params={'limit': 2, 'from_locator': 2})
    assert [block['locator_start'] for block in _json(second)['blocks']] == [2, 3]
    tail = client.get(f'/api/materials/{source_id}/blocks',
                      params={'limit': 2, 'from_locator': '6'})
    tail_body = _json(tail)
    assert [block['locator_start'] for block in tail_body['blocks']] == [6]
    assert tail_body['has_more'] is False


def test_blocks_locator_type_filter_and_prefix(client, world):
    source_id = world['ids']['txt']
    by_type = client.get(f'/api/materials/{source_id}/blocks',
                         params={'locator_type': 'line'})
    body = _json(by_type)
    assert [block['locator_start'] for block in body['blocks']] == [1, 2, 3, 4, 5, 6]
    assert body['has_more'] is False
    prefixed = client.get(f'/api/materials/{source_id}/blocks',
                          params={'from_locator': 'line:5', 'limit': 5})
    assert [block['locator_start'] for block in _json(prefixed)['blocks']] == [5, 6]
    bad = client.get(f'/api/materials/{source_id}/blocks',
                     params={'from_locator': 'abc'})
    assert bad.status_code == 400
    assert '定位' in _json(bad)['detail']


def test_blocks_limit_is_clamped(client, world):
    source_id = world['ids']['txt']
    body = _json(client.get(f'/api/materials/{source_id}/blocks', params={'limit': 0}))
    assert len(body['blocks']) == 1
    body = _json(client.get(f'/api/materials/{source_id}/blocks', params={'limit': 5000}))
    assert len(body['blocks']) == 6
    assert body['has_more'] is False


def test_blocks_truncate_long_evidence(client, world):
    ids = world['ids']
    body = _json(client.get(f'/api/materials/{ids["long"]}/blocks'))
    block = body['blocks'][0]
    assert block['evidence_id'] == ids['long_block']
    assert len(block['text']) == materials.MAX_BLOCK_CHARS
    assert block['truncated'] is True


def test_evidence_context_before_and_after(client, world):
    ids = world['ids']
    target = ids['txt_blocks'][2]
    response = client.get(f'/api/evidence/{target}/context',
                          params={'before': 1, 'after': 2})
    assert response.status_code == 200
    body = _json(response)
    assert body['evidence']['evidence_id'] == target
    assert body['evidence']['locator_start'] == 3
    assert [block['locator_start'] for block in body['before']] == [2]
    assert [block['locator_start'] for block in body['after']] == [4, 5]
    assert body['source']['id'] == ids['txt']
    assert body['source']['locator_types'] == ['line']


def test_evidence_context_missing_404(client):
    assert client.get('/api/evidence/99999/context').status_code == 404


def test_stale_evidence_is_not_readable(client, world):
    """P2-4：过期/软删证据不能再作为 target 打开，也不出现在 blocks 里。"""
    ids = world['ids']
    assert client.get(f'/api/evidence/{ids["txt_stale"]}/context').status_code == 404
    blocks = _json(client.get(f'/api/materials/{ids["txt"]}/blocks'))['blocks']
    assert ids['txt_stale'] not in [block['evidence_id'] for block in blocks]


def test_snapshot_context_excludes_stale_blocks(client, world):
    """P2-4：snapshot 复扫产生的历史证据（stale=1）不进入阅读上下文。"""
    ids = world['ids']
    response = client.get(f'/api/evidence/{ids["snap_current"]}/context',
                          params={'before': 1, 'after': 1})
    assert response.status_code == 200
    body = _json(response)
    assert body['evidence']['evidence_id'] == ids['snap_current']
    assert body['before'] == []
    assert body['after'] == []
    assert client.get(f'/api/evidence/{ids["snap_stale"]}/context').status_code == 404
    blocks = _json(client.get(f'/api/materials/{ids["snap"]}/blocks'))['blocks']
    assert ids['snap_stale'] not in [block['evidence_id'] for block in blocks]


def test_path_outside_data_dir_rejected(client, world):
    source_id = world['ids']['outside']
    assert client.get(f'/api/materials/{source_id}').status_code in (400, 404)
    assert client.get(f'/api/materials/{source_id}/blocks').status_code in (400, 404)
    assert client.get(f'/api/materials/{source_id}/page.png').status_code in (400, 404)


def test_missing_material_file_404(client, world):
    response = client.get(f'/api/materials/{world["ids"]["vanished"]}')
    assert response.status_code == 404
    assert '已不存在' in _json(response)['detail']


def test_non_pdf_page_png_415_fallback(client, world):
    response = client.get(f'/api/materials/{world["ids"]["txt"]}/page.png')
    assert response.status_code == 415
    assert response.headers['content-type'].startswith('application/json')
    body = _json(response)
    assert body['fallback'] == 'text'
    assert body['detail']


def test_text_pdf_page_png(client, world):
    response = client.get(f'/api/materials/{world["ids"]["pdf_text"]}/page.png',
                          params={'locator': 'page:2', 'dpi': 144})
    assert response.status_code == 200
    assert response.headers['content-type'] == 'image/png'
    assert response.headers['cache-control'] == 'no-store'
    assert response.content.startswith(b'\x89PNG\r\n\x1a\n')
    image = Image.open(io.BytesIO(response.content))
    assert image.size == (1224, 1584)


def test_page_png_dpi_is_clamped(client, world):
    source_id = world['ids']['pdf_text']
    low = client.get(f'/api/materials/{source_id}/page.png', params={'dpi': 10})
    high = client.get(f'/api/materials/{source_id}/page.png', params={'dpi': 999})
    assert Image.open(io.BytesIO(low.content)).size == (612, 792)
    assert Image.open(io.BytesIO(high.content)).size == (1700, 2200)


def test_page_png_rejects_bad_locator(client, world):
    source_id = world['ids']['pdf_text']
    assert client.get(f'/api/materials/{source_id}/page.png',
                      params={'locator': '99'}).status_code == 400
    assert client.get(f'/api/materials/{source_id}/page.png',
                      params={'locator': 'abc'}).status_code == 400
    assert client.get(f'/api/materials/{source_id}/page.png',
                      params={'locator': 'line:1'}).status_code == 400


def test_pdf_without_text_layer_415(client, world, monkeypatch):
    # Keep the text-only fallback contract independent of optional host PDFium.
    from app.memory import materials
    monkeypatch.setattr(materials, '_raster_with_pypdfium2', lambda *args, **kwargs: None)
    ids = world['ids']
    blank = client.get(f'/api/materials/{ids["pdf_blank"]}/page.png')
    assert blank.status_code == 415
    blank_body = _json(blank)
    assert blank_body['fallback'] == 'text'
    assert '文本层' in blank_body['detail']


def test_scanned_pdf_without_text_layer_415(client, world, monkeypatch):
    from app.memory import materials
    monkeypatch.setattr(materials, '_raster_with_pypdfium2', lambda *args, **kwargs: None)
    scanned = client.get(f'/api/materials/{world["ids"]["pdf_scanned"]}/page.png')
    assert scanned.status_code == 415
    assert _json(scanned)['fallback'] == 'text'
