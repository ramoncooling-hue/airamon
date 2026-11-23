"""app/memory/asr.py 单元测试：不依赖真实模型、不访问外网。

覆盖引擎缺失的降级路径、模型状态/下载（本地假镜像）、音频解码（stdlib wav 与
假 ffmpeg 子进程）、转写清洗、流式增量转写的去重与时间戳。真实模型冒烟测试
只在设置 MINDOCK_ASR_REAL=1 时运行，默认跳过。
"""

from __future__ import annotations

import functools
import http.server
import json
import math
import os
import struct
import sys
import threading
import types
import wave
from pathlib import Path

import pytest

try:  # ASR extras 可选；没有 numpy 时只跳过依赖 numpy 的用例。
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

from app.memory import asr, db

requires_numpy = pytest.mark.skipif(
    np is None, reason='numpy 未安装（ASR 依赖在 requirements-extensions.txt）')


# ---------------------------------------------------------------------------
# 测试基础设施
# ---------------------------------------------------------------------------

@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """把 app.memory.db.DATA_DIR 指到临时目录（models_dir 动态读取它）。"""
    root = tmp_path / 'data'
    monkeypatch.setattr(db, 'DATA_DIR', root)
    return root


def _touch_model(root, name='turbo'):
    directory = root / 'models' / ('faster-whisper-' + name)
    directory.mkdir(parents=True, exist_ok=True)
    for filename in asr.MODEL_SPECS[name]['files']:
        target = directory / filename
        if target.exists():
            continue
        if filename == 'config.json':
            target.write_text('{"model": "fake"}', encoding='utf-8')
        else:
            target.write_bytes(b'fake-model-bytes')
    return directory


def _patch_spec(monkeypatch, name='turbo', *, repo='test-model', files=None):
    spec = dict(asr.MODEL_SPECS[name])
    spec['repo'] = repo
    if files is not None:
        spec['files'] = tuple(files)
    monkeypatch.setitem(asr.MODEL_SPECS, name, spec)
    return spec


def _fake_engine(monkeypatch, model_cls):
    module = types.ModuleType('faster_whisper')
    module.WhisperModel = model_cls
    monkeypatch.setitem(sys.modules, 'faster_whisper', module)
    return module


def _no_engine(monkeypatch):
    """强制视为未安装引擎：sys.modules 里的 None 会遮蔽真实模块。"""
    monkeypatch.setitem(sys.modules, 'faster_whisper', None)


def _write_wav(path, *, seconds=1.0, rate=16000, channels=1, width=2,
               frequency=440.0, amplitude=0.5):
    frames = int(rate * seconds)
    peak = int(round(amplitude * (2 ** (8 * width - 1) - 1)))
    data = bytearray()
    for index in range(frames):
        value = int(round(peak * math.sin(2.0 * math.pi * frequency * index / rate)))
        for _ in range(channels):
            data += value.to_bytes(width, 'little', signed=True)
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(bytes(data))
    return path


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - 与基类签名一致
        pass


@pytest.fixture
def mirror_factory(tmp_path):
    """启动本地假镜像；start(files, repo) 返回 base_url，测试结束自动关闭。"""
    servers = []

    def start(files, repo='test-model'):
        root = tmp_path / ('mirror-%d' % len(servers))
        repo_dir = root / repo / 'resolve' / 'main'
        repo_dir.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            payload = content if isinstance(content, bytes) else str(content).encode('utf-8')
            (repo_dir / name).write_bytes(payload)
        handler = functools.partial(_QuietHandler, directory=str(root))
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return 'http://127.0.0.1:%d' % server.server_address[1]

    yield start

    for server, thread in servers:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


# ---------------------------------------------------------------------------
# 1. 引擎缺失时的降级路径 / 模型状态
# ---------------------------------------------------------------------------

def test_missing_engine_reports_install_hint(monkeypatch, data_root):
    _no_engine(monkeypatch)
    assert asr.engine_available() is False

    with pytest.raises(asr.AsrUnavailable) as excinfo:
        asr.Transcriber('turbo').load()
    message = str(excinfo.value)
    assert 'pip install' in message
    assert 'requirements-extensions.txt' in message


def test_engine_available_when_fake_module_present(monkeypatch):
    _fake_engine(monkeypatch, object)
    assert asr.engine_available() is True


def test_model_state_reports_missing_and_present(monkeypatch, data_root):
    _no_engine(monkeypatch)
    state = asr.model_state('turbo')
    assert state['name'] == 'turbo'
    assert state['present'] is False
    assert state['missing_files'] == list(asr.MODEL_SPECS['turbo']['files'])
    assert state['dir'] == str(data_root / 'models' / 'faster-whisper-turbo')
    assert state['size_bytes'] == asr.MODEL_SPECS['turbo']['size_bytes']
    assert state['note'] == asr.MODEL_SPECS['turbo']['note']
    assert state['engine_available'] is False

    _touch_model(data_root, 'turbo')
    state = asr.model_state('turbo')
    assert state['present'] is True
    assert state['missing_files'] == []


def test_models_dir_follows_db_data_dir(monkeypatch, data_root):
    first = asr.models_dir('small')
    other = data_root / 'other-data'
    monkeypatch.setattr(db, 'DATA_DIR', other)
    second = asr.models_dir('small')
    assert first == data_root / 'models' / 'faster-whisper-small'
    assert second == other / 'models' / 'faster-whisper-small'

    with pytest.raises(ValueError):
        asr.models_dir('nonexistent-model')


def test_transcriber_requires_downloaded_model(monkeypatch, data_root):
    class FakeModel:
        def __init__(self, *args, **kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError('模型缺失时不应构造引擎')

    _fake_engine(monkeypatch, FakeModel)
    transcriber = asr.Transcriber('turbo')
    with pytest.raises(asr.AsrUnavailable) as excinfo:
        transcriber.load()
    assert '尚未下载' in str(excinfo.value)


# ---------------------------------------------------------------------------
# 2. 模型下载（本地假镜像）
# ---------------------------------------------------------------------------

def test_download_model_downloads_files_and_reports_progress(
        monkeypatch, data_root, mirror_factory):
    files = {
        'config.json': b'{"model": "fake"}',
        'tokenizer.json': b'{"tokenizer": true}',
        'vocabulary.json': b'["hello", "world"]',
    }
    base_url = mirror_factory(files)
    _patch_spec(monkeypatch, files=tuple(files))

    progress = []
    directory = asr.download_model('turbo', progress.append, base_url=base_url, timeout=10)

    assert directory == data_root / 'models' / 'faster-whisper-turbo'
    for name, payload in files.items():
        assert (directory / name).read_bytes() == payload
    assert progress, '下载过程应回调进度'
    assert {'file', 'downloaded', 'total', 'percent'} <= set(progress[0])
    final = progress[-1]
    assert final['percent'] == 100.0
    assert final['file'] == 'vocabulary.json'
    assert final['downloaded'] == final['total'] == len(files['vocabulary.json'])

    # 第二次调用只补缺：全部已就绪，不再下载、不再回调
    progress.clear()
    asr.download_model('turbo', progress.append, base_url=base_url, timeout=10)
    assert progress == []


def test_download_model_resumes_from_part_file(monkeypatch, data_root, mirror_factory):
    files = {'config.json': b'{"model": "fake"}', 'model.bin': b'x' * 20}
    base_url = mirror_factory(files)
    _patch_spec(monkeypatch, files=tuple(files))

    directory = data_root / 'models' / 'faster-whisper-turbo'
    directory.mkdir(parents=True)
    part = directory / 'model.bin.part'
    part.write_bytes(b'x' * 10)  # 服务器不支持 Range 时应整文件重下而不是拼坏

    asr.download_model('turbo', base_url=base_url, timeout=10)
    assert (directory / 'model.bin').read_bytes() == files['model.bin']
    assert not part.exists()


def test_download_model_http_error_contains_url(monkeypatch, data_root, mirror_factory):
    base_url = mirror_factory({'config.json': b'{"model": "fake"}'})
    _patch_spec(monkeypatch, files=('config.json', 'missing.bin'))

    with pytest.raises(asr.AsrUnavailable) as excinfo:
        asr.download_model('turbo', base_url=base_url, timeout=10)
    message = str(excinfo.value)
    assert 'HTTP 404' in message
    assert base_url + '/test-model/resolve/main/missing.bin' in message


def test_download_model_rejects_corrupt_config(monkeypatch, data_root, mirror_factory):
    base_url = mirror_factory({'config.json': b'not-json-at-all'})
    _patch_spec(monkeypatch, files=('config.json',))

    with pytest.raises(asr.AsrUnavailable) as excinfo:
        asr.download_model('turbo', base_url=base_url, timeout=10)
    assert 'config.json 无法解析' in str(excinfo.value)


def test_download_model_connection_failure(monkeypatch, data_root):
    _patch_spec(monkeypatch, files=('config.json',))
    monkeypatch.setattr(asr, 'DOWNLOAD_ATTEMPTS', 1)

    with pytest.raises(asr.AsrUnavailable) as excinfo:
        asr.download_model('turbo', base_url='http://127.0.0.1:9', timeout=1)
    message = str(excinfo.value)
    assert '模型下载失败' in message
    assert 'http://127.0.0.1:9/' in message


# ---------------------------------------------------------------------------
# 3. 音频解码
# ---------------------------------------------------------------------------

def test_numpy_missing_degrades_without_breaking_cleanup(monkeypatch, tmp_path):
    _no_engine(monkeypatch)
    monkeypatch.setattr(asr, 'np', None)
    assert asr.engine_available() is False

    with pytest.raises(asr.AsrUnavailable) as excinfo:
        asr.Transcriber('turbo').load()
    assert 'numpy' in str(excinfo.value)

    wav = _write_wav(tmp_path / 'tone.wav', seconds=0.1)
    with pytest.raises(asr.AsrUnavailable) as excinfo:
        asr.decode_audio_file(wav)
    assert 'numpy' in str(excinfo.value)

    # 文本清洗是纯字符串处理，不依赖 numpy
    assert asr._clean_text('今天讲会计基本假设。。。') == '今天讲会计基本假设。'


@requires_numpy
def test_decode_wav_fallback_returns_16k_mono_float32(tmp_path):
    path = _write_wav(tmp_path / 'tone.wav', seconds=1.0, rate=16000,
                      channels=1, width=2, amplitude=0.5)
    samples = asr.decode_audio_file(path)
    assert isinstance(samples, np.ndarray)
    assert samples.dtype == np.float32
    assert samples.shape == (asr.SAMPLE_RATE,)
    assert 0.3 < float(np.abs(samples).max()) <= 1.0

    stereo = _write_wav(tmp_path / 'stereo.wav', seconds=0.5, rate=44100,
                        channels=2, width=2)
    samples = asr.decode_audio_file(stereo)
    assert samples.shape == (8000,)


@requires_numpy
def test_decode_wav_rejects_missing_file(tmp_path):
    with pytest.raises(ValueError):
        asr.decode_audio_file(tmp_path / 'nope.wav')


@requires_numpy
def test_decode_non_wav_uses_ffmpeg_pipe(tmp_path, monkeypatch):
    source = tmp_path / 'lecture.mp3'
    source.write_bytes(b'fake-mp3-bytes')
    captured = {}

    def fake_run(command, **kwargs):
        captured['command'] = command
        captured['kwargs'] = kwargs
        pcm = np.array([0.0, 0.25, -0.5], dtype=np.float32).tobytes()
        return types.SimpleNamespace(returncode=0, stdout=pcm, stderr=b'')

    monkeypatch.setattr(asr, '_ffmpeg_binary',
                        lambda ffmpeg=None, required=True: '/fake/ffmpeg')
    monkeypatch.setattr(asr, 'subprocess', types.SimpleNamespace(run=fake_run))

    samples = asr.decode_audio_file(source)
    assert [round(float(value), 3) for value in samples] == [0.0, 0.25, -0.5]

    command = captured['command']
    assert command[0] == '/fake/ffmpeg'
    assert '-i' in command and command[command.index('-i') + 1] == str(source)
    assert command[command.index('-ar') + 1] == '16000'
    assert command[command.index('-ac') + 1] == '1'
    assert command[-1] == 'pipe:1'
    assert captured['kwargs'].get('shell') is None
    assert captured['kwargs'].get('timeout') == asr.DECODE_TIMEOUT


@requires_numpy
def test_decode_non_wav_ffmpeg_failure_raises_unavailable(tmp_path, monkeypatch):
    source = tmp_path / 'broken.m4a'
    source.write_bytes(b'broken')

    monkeypatch.setattr(asr, '_ffmpeg_binary',
                        lambda ffmpeg=None, required=True: '/fake/ffmpeg')
    monkeypatch.setattr(asr, 'subprocess', types.SimpleNamespace(run=lambda *a, **k: (
        types.SimpleNamespace(returncode=1, stdout=b'', stderr=b'Invalid data found'))))

    with pytest.raises(asr.AsrUnavailable) as excinfo:
        asr.decode_audio_file(source)
    assert 'ffmpeg 退出码 1' in str(excinfo.value)


# ---------------------------------------------------------------------------
# 4. 转写（假引擎）
# ---------------------------------------------------------------------------

@requires_numpy
def test_transcriber_lazy_load_cleans_text_and_builds_prompt(monkeypatch, data_root):
    _touch_model(data_root, 'turbo')
    created = []
    calls = []

    class FakeModel:
        def __init__(self, model_path, *, device='cpu', compute_type='int8'):
            created.append({'path': model_path, 'device': device,
                            'compute_type': compute_type})

        def transcribe(self, audio, **kwargs):
            calls.append({'audio': audio, 'kwargs': kwargs})
            raw = [
                types.SimpleNamespace(start=0.0, end=1.0, text=' 谢谢观看。 ',
                                      avg_logprob=-0.5),
                types.SimpleNamespace(start=1.0, end=2.5, text='边际成本。。。',
                                      avg_logprob=-0.1),
            ]
            return iter(raw), types.SimpleNamespace(duration=2.5)

    _fake_engine(monkeypatch, FakeModel)
    transcriber = asr.Transcriber('turbo', prompt_terms=['边际成本', '机会成本'])
    assert created == []  # 懒加载：构造 Transcriber 不碰引擎

    pcm = np.zeros(asr.SAMPLE_RATE * 2, dtype=np.float32)
    segments, duration = transcriber.transcribe(pcm)
    assert duration == 2.5
    assert [segment.text for segment in segments] == ['边际成本。']
    assert segments[0].start == 1.0
    assert segments[0].confidence == round(math.exp(-0.1), 4)

    assert len(created) == 1
    assert created[0]['path'] == str(data_root / 'models' / 'faster-whisper-turbo')
    assert created[0]['device'] == 'cpu' and created[0]['compute_type'] == 'int8'

    kwargs = calls[0]['kwargs']
    assert kwargs['language'] == 'zh'
    assert kwargs['beam_size'] == 1
    assert kwargs['vad_filter'] is False
    assert '请用简体中文' in kwargs['initial_prompt']
    assert '术语：边际成本、机会成本。' in kwargs['initial_prompt']
    assert len(kwargs['initial_prompt']) <= asr.MAX_PROMPT_CHARS
    assert calls[0]['audio'].dtype == np.float32

    transcriber.transcribe(pcm)  # 第二次不再构造引擎
    assert len(created) == 1


@requires_numpy
def test_transcriber_empty_pcm_returns_early(monkeypatch, data_root):
    _no_engine(monkeypatch)
    transcriber = asr.Transcriber('turbo')
    segments, duration = transcriber.transcribe(np.zeros(0, dtype=np.float32))
    assert segments == [] and duration == 0.0


@requires_numpy
def test_transcriber_none_pcm_raises_value_error(monkeypatch, data_root):
    _no_engine(monkeypatch)
    with pytest.raises(ValueError):
        asr.Transcriber('turbo').transcribe(None)


# ---------------------------------------------------------------------------
# 5. 文本清洗
# ---------------------------------------------------------------------------

def test_clean_text_removes_hallucinations_and_collapses_punctuation():
    assert asr._clean_text('谢谢观看。请点赞订阅！') == ''
    assert asr._clean_text('字幕由 Amara.org 社区提供。') == ''
    assert asr._clean_text('本字幕由志愿者提供。') == ''
    assert asr._clean_text('今天讲会计基本假设。。。') == '今天讲会计基本假设。'
    assert asr._clean_text('  重点，，，内容。。。  ') == '重点，内容。'
    assert asr._clean_text('   ', ) == ''
    assert asr._clean_text(None) == ''
    assert asr._clean_text('，。！') == ''


# ---------------------------------------------------------------------------
# 6. 流式增量转写
# ---------------------------------------------------------------------------

@requires_numpy
def test_stream_session_dedupes_overlap_and_emits_monotonic_ms():
    class FakeStreamingTranscriber:
        texts = ['第一段语音内容，', '语音内容，第二段内容。', '段内容。第三段。']

        def __init__(self):
            self.window_sizes = []

        def transcribe(self, window, **kwargs):
            self.window_sizes.append(int(window.size))
            index = len(self.window_sizes) - 1
            text = self.texts[index]
            return [asr.Segment(0.0, 2.0, text, 0.9)], 2.0

    fake = FakeStreamingTranscriber()
    session = asr.StreamSession('turbo', transcriber=fake, window_s=6.0, hop_s=4.0)
    chunk = np.zeros(4 * asr.SAMPLE_RATE, dtype=np.float32)

    assert session.feed(chunk) == []
    first = session.feed(chunk)
    second = session.feed(chunk)
    tail = session.flush()

    assert fake.window_sizes == [96000, 96000, 64000]
    emitted = first + second + tail
    assert [segment.text for segment in emitted] == [
        '第一段语音内容，', '第二段内容。', '第三段。']
    assert ''.join(segment.text for segment in emitted) == '第一段语音内容，第二段内容。第三段。'
    starts = [segment.start for segment in emitted]
    ends = [segment.end for segment in emitted]
    assert starts == [0, 4000, 8000]
    assert ends == [2000, 6000, 10000]
    assert starts == sorted(starts)
    assert all(isinstance(value, int) for value in starts + ends)
    assert all(end >= start for start, end in zip(starts, ends))


@requires_numpy
def test_stream_session_tolerates_empty_and_non_numeric_chunks():
    class SilentTranscriber:
        def transcribe(self, window, **kwargs):
            return [asr.Segment(0.0, 1.0, '正常。', None)], 1.0

    session = asr.StreamSession('turbo', transcriber=SilentTranscriber(),
                                window_s=1.0, hop_s=1.0)
    assert session.feed(None) == []
    assert session.feed([]) == []
    assert session.feed(np.array([], dtype=np.float32)) == []
    assert session.feed('not-audio') == []
    assert session.feed(object()) == []

    emitted = session.feed(np.zeros(asr.SAMPLE_RATE, dtype=np.float32))
    assert [segment.text for segment in emitted] == ['正常。']
    assert session.flush() == []


@requires_numpy
@pytest.mark.parametrize('sample_count', [1, 3200, 6399])
def test_stream_session_flush_transcribes_every_nonempty_tail(sample_count):
    calls = []

    class RecordingTranscriber:
        def transcribe(self, window, **kwargs):
            calls.append(int(window.size))
            return [asr.Segment(0, window.size / asr.SAMPLE_RATE, 'tail', 0.9)], 0.0

    session = asr.StreamSession('turbo', transcriber=RecordingTranscriber(),
                                window_s=6.0, hop_s=4.0)
    short = np.zeros(sample_count, dtype=np.float32)
    assert session.feed(short) == []
    assert [segment.text for segment in session.flush()] == ['tail']
    assert session.flush() == []
    assert calls == [sample_count]


@requires_numpy
def test_stream_session_flush_failure_keeps_audio_and_dedup_state():
    calls = []

    class RetryTranscriber:
        def transcribe(self, window, **kwargs):
            calls.append(window.copy())

            def segments():
                yield asr.Segment(0, 0.1, 'tail', 0.9)
                if len(calls) == 1:
                    raise asr.AsrUnavailable('retry tail')

            return segments(), 0.2

    session = asr.StreamSession('small', transcriber=RetryTranscriber())
    pcm = np.ones(3200, dtype=np.float32)
    session.feed(pcm)
    with pytest.raises(asr.AsrUnavailable, match='retry tail'):
        session.flush()
    assert session._buffer.size == pcm.size
    assert session._consumed == 0
    assert session._tail == ''
    assert session._last_end == 0
    assert [segment.text for segment in session.flush()] == ['tail']
    assert len(calls) == 2
    assert all(np.array_equal(audio, pcm) for audio in calls)
    assert session.flush() == []


@requires_numpy
def test_stream_session_feed_failure_keeps_all_unreturned_windows():
    class RetryTranscriber:
        calls = 0

        def transcribe(self, window, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise asr.AsrUnavailable('second window failed')
            return [asr.Segment(0, 0.1, 'retained audio', 0.9)], 0.1

    session = asr.StreamSession('small', transcriber=RetryTranscriber(), window_s=1, hop_s=1)
    pcm = np.ones(2 * asr.SAMPLE_RATE, dtype=np.float32)
    with pytest.raises(asr.AsrUnavailable, match='second window failed'):
        session.feed(pcm)
    assert np.array_equal(session._buffer, pcm)
    assert session._consumed == 0
    assert session._tail == ''
    assert session._last_end == 0
    assert [segment.text for segment in session.flush()] == ['retained audio']
    assert session.flush() == []


@requires_numpy
def test_stream_session_reset_forgets_buffer():
    class SilentTranscriber:
        def transcribe(self, window, **kwargs):
            return [], 0.0

    session = asr.StreamSession('turbo', transcriber=SilentTranscriber(),
                                window_s=2.0, hop_s=1.0)
    session.feed(np.ones(asr.SAMPLE_RATE, dtype=np.float32))
    session.reset()
    assert session.flush() == []


# ---------------------------------------------------------------------------
# 7. 真实模型冒烟（默认跳过）
# ---------------------------------------------------------------------------

@requires_numpy
@pytest.mark.skipif(not os.environ.get('MINDOCK_ASR_REAL'),
                    reason='设置 MINDOCK_ASR_REAL=1 才跑真实模型冒烟测试')
def test_real_model_smoke(tmp_path):
    name = os.environ.get('MINDOCK_ASR_MODEL', 'small')
    directory = Path(os.environ.get('MINDOCK_ASR_MODEL_DIR')
                     or asr.models_dir(name)).expanduser()
    if not (directory / 'model.bin').is_file():
        pytest.skip('真实模型不存在：%s' % directory)
    if not asr.engine_available():
        pytest.skip('faster-whisper 未安装')

    path = _write_wav(tmp_path / 'tone.wav', seconds=1.0)
    samples = asr.decode_audio_file(path)
    transcriber = asr.Transcriber(name, model_dir=directory)
    segments, duration = transcriber.transcribe(samples, beam_size=1)
    assert duration > 0
    assert isinstance(segments, list)
