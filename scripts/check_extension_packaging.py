"""Report extension dependency declarations; never claims native acceptance."""
import argparse
import json
from pathlib import Path
import re
import importlib.metadata
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ('mcp', 'sqlite-vec')


def require_declarations(target, root=ROOT):
    report = inspect(root)['targets'][target]
    if report['dependency_declarations'] != 'matched':
        raise RuntimeError(f'{target} extension dependency lock is missing or mismatched')


def verify_installed_lock(lock):
    """Fail before freezing when the selected build interpreter differs from its lock."""
    mismatched = []
    for name, expected in pins(lock).items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        if actual != expected:
            mismatched.append(name)
    if mismatched:
        raise RuntimeError('Build environment does not match lock: ' + ', '.join(mismatched))


def collect_macos_extensions(root=ROOT):
    """PyInstaller manifest: keep native vec0 at sqlite_vec's package-relative path."""
    from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

    require_declarations('macos', root)
    data, binaries, hidden = [], [], []
    for distribution in PACKAGES:
        data += copy_metadata(distribution, recursive=True)
    for module in ('mcp', 'mcp_types', 'httpx2', 'httpcore2', 'truststore',
                   'jsonschema', 'jsonschema_specifications', 'referencing', 'sqlite_vec'):
        hidden += collect_submodules(module, filter=lambda name: not any(
            name == excluded or name.startswith(excluded + '.')
            for excluded in ('mcp.cli', 'httpx2.websockets')))
        data += collect_data_files(module)
    binaries += collect_dynamic_libs('sqlite_vec')
    if not any(Path(source).name == 'vec0.dylib' for source, _ in binaries):
        raise RuntimeError('sqlite-vec macOS arm64 native library missing')
    return data, binaries, hidden


def collect_macos_asr():
    """Bundle the offline ASR engine and VAD assets, not user model downloads."""
    from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

    data = []
    for distribution in ('faster-whisper', 'ctranslate2', 'onnxruntime', 'av',
                         'tokenizers', 'huggingface-hub', 'numpy'):
        data += copy_metadata(distribution, recursive=True)
    binaries, hidden = [], []
    for module in ('faster_whisper', 'ctranslate2', 'onnxruntime', 'av', 'tokenizers',
                   'huggingface_hub', 'numpy'):
        data += collect_data_files(module)
        binaries += collect_dynamic_libs(module)
    hidden += ['faster_whisper', 'faster_whisper.vad', 'faster_whisper.audio',
               'faster_whisper.transcribe', 'ctranslate2', 'onnxruntime',
               'onnxruntime.capi._pybind_state', 'av', 'tokenizers',
               'huggingface_hub', 'numpy']
    return data, binaries, hidden


def asr_runtime_probe():
    """Exercise bundled native audio/VAD libraries without downloading a model."""
    import io
    import wave
    import ctranslate2
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import get_vad_model

    stream = io.BytesIO()
    with wave.open(stream, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b'\0\0' * 1600)
    stream.seek(0)
    decoded = decode_audio(stream)
    assert len(decoded) == 1600
    assert get_vad_model() is not None
    compute_types = sorted(ctranslate2.get_supported_compute_types('cpu'))
    return {'versions': {name: importlib.metadata.version(name) for name in
                        ('faster-whisper', 'ctranslate2', 'onnxruntime', 'numpy', 'av', 'tokenizers')},
            'vad_loaded': True, 'audio_decode_samples': len(decoded),
            'cpu_compute_types': compute_types, 'model_downloaded': False,
            'transcription_verified': False, 'frozen': bool(getattr(sys, 'frozen', False))}


def reader_runtime_probe():
    """Render an in-memory PDF using the delivered native PDFium library."""
    import io
    import pypdfium2
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    with pypdfium2.PdfDocument(stream.getvalue()) as document:
        page = document[0]
        bitmap = page.render(scale=1)
        try:
            rendered = bitmap.to_pil()
            assert rendered.size == (72, 72)
        finally:
            bitmap.close()
            page.close()
    return {'pypdfium2': importlib.metadata.version('pypdfium2'), 'page_rendered': True,
            'frozen': bool(getattr(sys, 'frozen', False))}


def verify_extension_payload(base, target):
    """Inspect files only; do not claim frozen imports or native execution succeeded."""
    native = base / 'sqlite_vec' / ('vec0.dll' if target == 'windows' else 'vec0.dylib')
    required = [native]
    if target == 'windows':
        required += [base / 'mcp/client/stdio.py', base / 'mcp/client/streamable_http.py',
                     base / 'pywin32.pth', base / 'win32/lib/pywin32_bootstrap.py',
                     base / 'pywin32_system32/pywintypes312.dll',
                     base / 'pywin32_system32/pythoncom312.dll']
    missing = [str(path.relative_to(base)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError('Extension payload missing: ' + ', '.join(missing))
    magic = native.read_bytes()[:4]
    if target == 'windows' and magic[:2] != b'MZ':
        raise RuntimeError('sqlite-vec payload is not a Windows PE library')
    if target == 'macos' and magic not in {b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe'}:
        raise RuntimeError('sqlite-vec payload is not a Mach-O library')
    return {'target': target, 'payload_files': 'present', 'native_runtime_acceptance': 'not_checked'}


def runtime_probe():
    """Run in the delivered interpreter; only imports and an in-memory SQLite database."""
    import mcp.client.stdio
    import mcp.client.streamable_http
    import sqlite3
    import sqlite_vec
    import jsonschema

    # Resolving a standard schema exercises jsonschema-specifications package data.
    jsonschema.Draft202012Validator.check_schema({'type': 'object', 'properties': {'ok': {'type': 'boolean'}}})
    with sqlite3.connect(':memory:') as connection:
        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
        native_version, distance = connection.execute(
            "select vec_version(), vec_distance_L2('[0,0]', '[3,4]')").fetchone()
    version = importlib.metadata.version('sqlite-vec')
    if native_version.lstrip('v') != version or distance != 5.0:
        raise RuntimeError('sqlite-vec native version/query probe failed')
    return {'versions': {name: importlib.metadata.version(name) for name in PACKAGES},
            'mcp_transports_imported': ['stdio', 'streamable_http'],
            'sqlite_vec_version': native_version, 'sqlite_vec_distance': distance,
            'frozen': bool(getattr(sys, 'frozen', False)),
            'scope': 'imports/schema/native query only; not MCP handshake or installer acceptance'}


def pins(path):
    result = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        match = re.match(r'^([A-Za-z0-9_.-]+)==([^\s;]+)', line.strip())
        if match:
            result[re.sub(r'[-_.]+', '-', match[1]).lower()] = match[2]
    return result


def inspect(root=ROOT):
    requested = pins(root / 'requirements-extensions.txt')
    targets = {}
    for name, relative in (
        ('macos', 'desktop/requirements-lock.txt'),
        ('windows', 'desktop/windows/requirements-lock.txt'),
    ):
        lock_path = root / relative
        locked = pins(lock_path) if lock_path.is_file() else {}
        missing = [p for p in PACKAGES if p not in locked]
        mismatched = [p for p in PACKAGES if p in locked and locked[p] != requested.get(p)]
        targets[name] = {
            'dependency_declarations': 'missing' if missing or mismatched else 'matched',
            'missing': missing, 'mismatched': mismatched,
            'native_runtime_acceptance': 'not_checked',
        }
    return {'scope': 'dependency declarations only; not installer or native runtime verification',
            'requested': {p: requested.get(p) for p in PACKAGES}, 'targets': targets}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-declarations', action='store_true',
                        help='Exit nonzero when optional dependencies are absent from a desktop lock.')
    parser.add_argument('--extension-probe', action='store_true',
                        help='Import SDK transports and run sqlite-vec in memory; never open user data.')
    parser.add_argument('--asr-probe', action='store_true',
                        help='Decode synthetic audio and load bundled VAD without model downloads.')
    parser.add_argument('--reader-probe', action='store_true', help='Render a synthetic PDF page in memory.')
    args = parser.parse_args()
    if args.reader_probe:
        print(json.dumps(reader_runtime_probe(), indent=2))
        return 0
    if args.asr_probe:
        print(json.dumps(asr_runtime_probe(), indent=2))
        return 0
    if args.extension_probe:
        print(json.dumps(runtime_probe(), indent=2))
        return 0
    report = inspect()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(args.require_declarations and any(
        t['dependency_declarations'] != 'matched' for t in report['targets'].values()))


if (getattr(sys, 'frozen', False) and sys.argv[1:] in (['--extension-probe'], ['--asr-probe'], ['--reader-probe'])) or (
        not getattr(sys, 'frozen', False) and __name__ == '__main__'):
    raise SystemExit(main())
