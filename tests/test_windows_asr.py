"""Windows packaging checks use synthetic payloads and never open user data."""

import struct
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import build_windows, verify_windows


def _pe(path, machine=0x8664):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = bytearray(128)
    content[:2] = b'MZ'
    struct.pack_into('<I', content, 0x3c, 64)
    content[64:68] = b'PE\0\0'
    struct.pack_into('<H', content, 68, machine)
    path.write_bytes(content)


def _asr_payload(root):
    site = root / 'python/Lib/site-packages'
    for module in ('faster_whisper', 'ctranslate2', 'onnxruntime', 'numpy',
                   'av', 'tokenizers', 'huggingface_hub'):
        path = site / module / '__init__.py'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('')
    assets = site / 'faster_whisper/assets'
    assets.mkdir()
    (assets / 'silero_vad_v6.onnx').write_bytes(b'vad-model')
    for relative in ('ctranslate2/_ext.cp312-win_amd64.pyd',
                     'onnxruntime/capi/onnxruntime_pybind11_state.pyd',
                     'av/_core.cp312-win_amd64.pyd',
                     'tokenizers/tokenizers.pyd', 'numpy/_core/_multiarray_umath.pyd',
                     'av.libs/avcodec-62-test.dll', 'numpy.libs/libscipy_openblas-test.dll',
                     'ctranslate2/ctranslate2.dll', 'onnxruntime/capi/onnxruntime.dll'):
        _pe(site / relative)
    _pe(root / 'bin/ffmpeg.exe')
    return site


def test_windows_asr_declaration_check_rejects_incomplete_lock(tmp_path):
    lock = tmp_path / 'requirements-lock.txt'
    lock.write_text('mcp==2.3.0\nsqlite-vec==0.1.9\n')
    with pytest.raises(RuntimeError, match='ASR'):
        build_windows.require_asr_declarations(lock)


def test_windows_asr_payload_checks_native_binaries_and_vad(tmp_path):
    site = _asr_payload(tmp_path)
    report = verify_windows.inspect_asr(tmp_path)
    assert report['native_runtime_acceptance'] == 'not_checked'
    assert report['speech_models_bundled'] is False
    (site / 'faster_whisper/assets/silero_vad_v6.onnx').unlink()
    with pytest.raises((AssertionError, RuntimeError), match='VAD'):
        verify_windows.inspect_asr(tmp_path)


def test_windows_asr_payload_rejects_non_windows_native_library(tmp_path):
    site = _asr_payload(tmp_path)
    _pe(site / 'ctranslate2/_ext.cp312-win_amd64.pyd', machine=0xaa64)
    with pytest.raises((AssertionError, RuntimeError), match='x64'):
        verify_windows.inspect_asr(tmp_path)


@pytest.mark.parametrize('directory', ['av.libs', 'numpy.libs'])
def test_windows_asr_payload_requires_sidecar_dlls(tmp_path, directory):
    site = _asr_payload(tmp_path)
    next((site / directory).glob('*.dll')).unlink()
    with pytest.raises(AssertionError, match='sidecar'):
        verify_windows.inspect_asr(tmp_path)


def test_windows_asr_payload_checks_sidecar_architecture(tmp_path):
    site = _asr_payload(tmp_path)
    _pe(next((site / 'av.libs').glob('*.dll')), machine=0xaa64)
    with pytest.raises(AssertionError, match='x64'):
        verify_windows.inspect_asr(tmp_path)


def test_windows_build_requires_asr_before_downloading_or_copying(tmp_path):
    root = tmp_path / 'root'
    windows = root / 'desktop/windows'
    windows.mkdir(parents=True)
    (windows / 'runtime-artifacts.json').write_text('{}')
    (windows / 'requirements-lock.txt').write_text('mcp==2.3.0\nsqlite-vec==0.1.9\n')
    with patch.object(build_windows, 'ROOT', root), \
         patch.object(build_windows, 'require_declarations'), \
         patch.object(build_windows, 'run') as run:
        with pytest.raises(RuntimeError, match='ASR'):
            build_windows.prepare(tmp_path / 'output')
        run.assert_not_called()


def test_windows_ffmpeg_is_collected_from_locked_wheel(tmp_path):
    site = tmp_path / 'site'
    binary = site / 'imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe'
    _pe(binary)
    stage = tmp_path / 'backend'
    result = build_windows.collect_ffmpeg(site, stage)
    assert result == stage / 'bin/ffmpeg.exe'
    assert result.read_bytes() == binary.read_bytes()


def test_windows_asr_probe_is_model_free_and_uses_temporary_audio():
    source = (build_windows.ROOT / 'desktop/windows/asr_probe.py').read_text()
    assert 'TemporaryDirectory' in source
    assert 'get_speech_timestamps' in source
    assert 'decode_audio_file' in source
    assert 'download_model(' not in source
    assert 'WhisperModel(' not in source
