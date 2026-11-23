from pathlib import Path

from scripts import check_extension_packaging as packaging


ROOT = Path(__file__).resolve().parents[1]


def test_asr_dependency_closure_is_locked():
    lock = packaging.pins(ROOT / 'desktop/requirements-lock.txt')
    for name in ('faster-whisper', 'ctranslate2', 'onnxruntime', 'numpy', 'av',
                 'tokenizers', 'huggingface-hub', 'flatbuffers', 'protobuf',
                 'filelock', 'fsspec', 'hf-xet', 'tqdm'):
        assert name in lock, name


def test_frozen_spec_keeps_asr_and_numpy():
    spec = (ROOT / 'desktop/backend.spec').read_text()
    assert 'collect_macos_asr' in spec
    excludes = spec.split('excludes=')[1].split(']')[0]
    assert "'numpy'" not in excludes


def test_macos_declares_microphone_reason():
    assert 'NSMicrophoneUsageDescription' in (ROOT / 'scripts/build_macos.py').read_text()


def test_asr_probe_loads_local_vad_without_download():
    result = packaging.asr_runtime_probe()
    assert result['vad_loaded'] is True
    assert result['audio_decode_samples'] == 1600
    assert result['model_downloaded'] is False
    assert result['transcription_verified'] is False


def test_reader_probe_renders_without_user_files():
    import pytest
    pytest.importorskip('pypdfium2')
    assert packaging.reader_runtime_probe()['page_rendered'] is True
