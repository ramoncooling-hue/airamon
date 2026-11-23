"""Listening transcription engine (旁听转录 Phase 1): local ASR via faster-whisper.

Engine layer only: no routes, no database writes, no UI. The API layer owns
sessions and persistence; this module turns PCM audio into timestamped text.

Design notes:
- faster-whisper / ctranslate2 / onnxruntime / numpy are optional extras
  (requirements-extensions.txt). Importing app.memory.asr never imports the
  engine, so the app boots without the extras; every engine touchpoint raises
  AsrUnavailable with a user-readable installation hint.
- Model files are fetched directly over HTTP from hf-mirror.com: the
  huggingface_hub Xet download path hangs behind the firewall in mainland
  China, so snapshot_download() is deliberately not used. Downloads go to
  .part files and resume with a Range request when the mirror allows it.
- Audio always reaches the model as float32 mono 16 kHz numpy. Non-WAV files
  are decoded by an ffmpeg subprocess (pipe:1, never shell=True); .wav uses
  the standard library so a missing ffmpeg does not break the common case.
  faster-whisper's own av-based decoder is not used because av 19 is
  incompatible with faster-whisper 1.2.1.
- Long listening sessions stream: StreamSession keeps a window/hop overlap and
  removes character-level suffix/prefix overlap, so one sentence is never
  emitted twice.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path

from . import db

try:  # numpy ships with the ASR extras; the module still imports without it.
    import numpy as np
except ImportError:  # pragma: no cover - lean environments without ASR extras
    np = None

LOGGER = logging.getLogger('mindock.asr')

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 4
DEFAULT_WINDOW_SECONDS = 6.0
DEFAULT_HOP_SECONDS = 4.0
MIN_FLUSH_SECONDS = 0.4
MAX_PROMPT_CHARS = 200
DEFAULT_PROMPT = '以下是普通话中文讲座，请用简体中文准确记录。'
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_CHUNK = 256 * 1024
DEFAULT_BASE_URL = 'https://hf-mirror.com'
DECODE_TIMEOUT = 600
TAIL_CHARS = 60
MIN_OVERLAP_CHARS = 2
FFMPEG_CANDIDATES = (
    '/opt/homebrew/bin/ffmpeg',
    '/usr/local/bin/ffmpeg',
    '/usr/bin/ffmpeg',
    'C:/ffmpeg/bin/ffmpeg.exe',
)
INSTALL_HINT = ('安装方式：pip install -r requirements-extensions.txt'
                '（faster-whisper、ctranslate2、onnxruntime、numpy）。')

MODEL_SPECS: dict[str, dict] = {
    'turbo': {
        'label': '高精度（large-v3-turbo）',
        'repo': 'mobiuslabsgmbh/faster-whisper-large-v3-turbo',
        'files': ('config.json', 'tokenizer.json', 'vocabulary.json',
                  'preprocessor_config.json', 'model.bin'),
        'size_bytes': 1_621_665_983,
        'note': '中文准确率最好，但 CPU 上转写速度约为实时的 1 倍（6 秒音频要 4-9 秒），适合导入音频或课后精校，不建议用于实时旁听。',
    },
    'small': {
        'label': '快速（small）',
        'repo': 'Systran/faster-whisper-small',
        'files': ('config.json', 'tokenizer.json', 'vocabulary.txt', 'model.bin'),
        'size_bytes': 486_212_372,
        'note': '实时旁听默认模型：CPU 上 6 秒音频约 1-2 秒出字，首次使用需下载约 480MB。',
    },
}

# Whisper 在静音/片头片尾常见的幻听串；命中后整句丢弃（清洗后只剩标点即视为幻听）。
HALLUCINATION_RE = re.compile(
    r'(?:字幕由[^。！？\n]{0,40}(?:提供|翻译|制作|校对)'
    r'|本字幕[^。！？\n]{0,40}'
    r'|(?:谢谢|感谢)(?:观看|收看|收听)[^。！？\n]{0,12}'
    r'|(?:请)?(?:点赞|订阅|关注|转发)[^。！？\n]{0,20}'
    r'|(?:优优独播剧场|明镜与点点栏目|Amara\.org)[^。！？\n]{0,20})',
    re.IGNORECASE,
)
PUNCTUATION_RUN = re.compile(r'([，。！？、；：,.!?;:])\1+')
CONTENT_CHAR = re.compile(r'[\w\u4e00-\u9fff]')
EDGE_PUNCTUATION = ' \t\r\n，。！？、；：,.!?;:'


class AsrUnavailable(RuntimeError):
    """ASR 功能在当前环境不可用；消息可直接展示给用户（含安装提示）。"""


@dataclass
class Segment:
    """一段转写结果。

    start/end: Transcriber 返回秒（与原文本一致）；StreamSession 返回会话
    绝对毫秒（int）。text 已清洗；confidence 为 0-1 的近似置信度，取不到为 None。
    """

    start: float
    end: float
    text: str
    confidence: float | None = None


def _spec(name) -> dict:
    key = str(name or '').strip()
    spec = MODEL_SPECS.get(key)
    if spec is None:
        choices = '/'.join(MODEL_SPECS)
        raise ValueError(f'未知语音模型：{key or "(空)"}（可选：{choices}）')
    return spec


def _numpy():
    if np is None:
        raise AsrUnavailable(f'本地语音识别需要 numpy。{INSTALL_HINT}')
    return np


def _whisper_available() -> bool:
    """True when faster_whisper can be imported without importing it now."""
    module = sys.modules.get('faster_whisper')
    if module is not None:
        return True
    try:
        return importlib.util.find_spec('faster_whisper') is not None
    except (ImportError, ValueError):
        return False


def engine_available() -> bool:
    """是否有可用的本地引擎（numpy + faster_whisper 都可导入）。不触发导入。"""
    return np is not None and _whisper_available()


def _import_whisper():
    if not _whisper_available():
        raise AsrUnavailable(f'本地语音识别引擎未安装。{INSTALL_HINT}')
    try:
        return importlib.import_module('faster_whisper')
    except ImportError as exc:
        raise AsrUnavailable(f'本地语音识别引擎导入失败：{exc}。{INSTALL_HINT}') from exc


def models_dir(name: str = 'turbo') -> Path:
    """模型目录：<DATA_DIR>/models/faster-whisper-<name>。

    DATA_DIR 每次调用时从 app.memory.db 动态读取（遵循 app/runtime.data_dir
    的既有约定），测试和桌面版切换数据根时无需重新加载模块。
    """
    _spec(name)
    return Path(db.DATA_DIR) / 'models' / f'faster-whisper-{str(name).strip()}'


def _file_ready(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def model_state(name: str = 'turbo') -> dict:
    """模型是否已下载就绪：present/missing_files/dir/size_bytes/note/engine_available。"""
    spec = _spec(name)
    directory = models_dir(name)
    missing = [item for item in spec['files'] if not _file_ready(directory / item)]
    return {
        'name': str(name).strip(),
        'label': spec['label'],
        'note': spec['note'],
        'present': not missing,
        'dir': str(directory),
        'size_bytes': int(spec['size_bytes']),
        'missing_files': missing,
        'engine_available': engine_available(),
    }


def _model_url(base_url: str, repo: str, filename: str) -> str:
    return f'{str(base_url).rstrip("/")}/{str(repo).strip("/")}/resolve/main/{filename}'


def _report_progress(progress, filename: str, downloaded: int, total: int | None) -> None:
    if progress is None:
        return
    percent = round(downloaded * 100.0 / total, 1) if total else None
    payload = {
        'file': filename,
        'downloaded': int(downloaded),
        'total': None if total is None else int(total),
        'percent': percent,
    }
    try:
        progress(payload)
    except Exception:  # 进度回调是旁路，不能影响下载
        LOGGER.warning('ASR download progress callback failed', exc_info=True)


def _download_file(url: str, target: Path, *, progress, timeout: float) -> None:
    """下载单个模型文件到 target；.part 断点续传 + 有限重试。失败抛 AsrUnavailable。"""
    part = target.with_name(target.name + '.part')
    last_error = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        offset = part.stat().st_size if _file_ready(part) else 0
        headers = {'User-Agent': 'MinDock-ASR/1.0'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, 'status', 200) or 200)
                if status not in (200, 206):
                    raise AsrUnavailable(f'模型下载失败（HTTP {status}）：{url}，请稍后重试。')
                if status == 200:
                    offset = 0  # 服务器忽略 Range，只能整文件重下
                length = response.headers.get('Content-Length')
                try:
                    size = int(length) if length else None
                except (TypeError, ValueError):
                    size = None
                total = size + offset if (size is not None and status == 206) else size
                downloaded = offset
                with part.open('ab' if offset else 'wb') as handle:
                    while True:
                        chunk = response.read(DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        handle.write(chunk)
                        downloaded += len(chunk)
                        _report_progress(progress, target.name, downloaded, total)
                if total is not None and downloaded < total:
                    raise OSError('连接提前结束，文件不完整')
            os.replace(part, target)
            final_size = target.stat().st_size
            _report_progress(progress, target.name, final_size, final_size)
            return
        except AsrUnavailable:
            raise
        except urllib.error.HTTPError as exc:
            if 400 <= int(exc.code) < 500:
                raise AsrUnavailable(
                    f'模型下载失败（HTTP {exc.code}）：{url}。请确认镜像可用或稍后重试。') from exc
            last_error = exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
        if attempt < DOWNLOAD_ATTEMPTS:
            LOGGER.warning('ASR download retry %s/%s for %s: %s',
                           attempt, DOWNLOAD_ATTEMPTS, url, last_error)
            time.sleep(0.5 * attempt)
    raise AsrUnavailable(
        f'模型下载失败：{url}（{last_error}）。请检查网络，或通过 base_url 换用其他镜像后重试。')


def download_model(name: str = 'turbo', progress=None, *,
                   base_url: str = DEFAULT_BASE_URL, timeout: float = 30) -> Path:
    """下载缺失的模型文件到 models_dir(name)，返回模型目录。

    只补缺：已存在且非空的文件跳过；.part 断点续传（服务器支持 Range 时）。
    失败模式：网络/镜像不可用、HTTP 4xx/5xx、下载不完整或 config.json 损坏
    都会抛 AsrUnavailable，消息包含 URL 与处理建议。
    """
    spec = _spec(name)
    directory = models_dir(name)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise AsrUnavailable(f'无法创建模型目录：{directory}（{exc}）') from exc
    host = str(base_url).rstrip('/')
    for filename in spec['files']:
        target = directory / filename
        if _file_ready(target):
            continue
        _download_file(_model_url(host, spec['repo'], filename), target,
                       progress=progress, timeout=timeout)
    try:
        json.loads((directory / 'config.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise AsrUnavailable(f'模型文件不完整：{directory} 的 config.json 无法解析（{exc}）。') from exc
    LOGGER.info('ASR model %s ready at %s', name, directory)
    return directory


def _ffmpeg_binary(ffmpeg=None, *, required: bool = True):
    if ffmpeg:
        candidate = Path(str(ffmpeg)).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        if required:
            raise AsrUnavailable(f'指定的 ffmpeg 不存在或不可执行：{ffmpeg}。')
        return None
    found = shutil.which('ffmpeg')
    if found:
        return found
    for candidate in FFMPEG_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    if required:
        raise AsrUnavailable(
            '未找到 ffmpeg，无法解码该音频格式。请安装 ffmpeg（macOS: brew install ffmpeg）'
            '，或改用 .wav 文件。')
    return None


def _slice_float_view(raw: bytes):
    np_mod = _numpy()
    usable = len(raw) - (len(raw) % SAMPLE_WIDTH)
    if usable <= 0:
        return np_mod.empty(0, dtype=np_mod.float32)
    return np_mod.frombuffer(raw[:usable], dtype=np.float32).copy()


def decode_audio_file(path, *, ffmpeg=None):
    """把音频文件解码为 float32 单声道 16 kHz 的 numpy 数组。

    .wav/.wave 走标准库 wave（无 ffmpeg 也能用）；其他格式调用 ffmpeg 子进程
    （pipe:1，禁止 shell=True）。失败模式：文件不存在抛 ValueError；numpy 缺失、
    ffmpeg 缺失、解码失败或没有音轨抛 AsrUnavailable。
    """
    np_mod = _numpy()
    source = Path(str(path or '')).expanduser()
    if not source.is_file():
        raise ValueError(f'音频文件不存在：{path}')
    if source.suffix.lower() in ('.wav', '.wave'):
        samples = _decode_wav(source)
        return np_mod.ascontiguousarray(samples, dtype=np_mod.float32)
    binary = _ffmpeg_binary(ffmpeg)
    command = [binary, '-nostdin', '-loglevel', 'error', '-i', str(source),
               '-f', 'f32le', '-ac', '1', '-ar', str(SAMPLE_RATE), 'pipe:1']
    try:
        completed = subprocess.run(command, capture_output=True, check=False,
                                   timeout=DECODE_TIMEOUT)
    except FileNotFoundError as exc:
        raise AsrUnavailable(f'ffmpeg 无法执行：{binary}。') from exc
    except subprocess.TimeoutExpired as exc:
        raise AsrUnavailable('音频解码超时（文件可能过大或已损坏）。') from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode('utf-8', 'replace').strip().splitlines()
        reason = ' '.join(detail[-3:]) if detail else '未知错误'
        raise AsrUnavailable(f'音频解码失败（ffmpeg 退出码 {completed.returncode}）：{reason}')
    samples = _slice_float_view(completed.stdout)
    if samples.size == 0:
        raise AsrUnavailable(f'音频文件没有可解码的音轨：{source.name}')
    return np_mod.ascontiguousarray(samples, dtype=np_mod.float32)


def _pcm_to_float(frames: bytes, width: int):
    np_mod = _numpy()
    if width == 1:
        values = np_mod.frombuffer(frames, dtype=np_mod.uint8).astype(np_mod.float32)
        return (values - 128.0) / 128.0
    if width == 2:
        return np_mod.frombuffer(frames, dtype='<i2').astype(np_mod.float32) / 32768.0
    if width == 3:
        raw = np_mod.frombuffer(frames, dtype=np_mod.uint8)
        usable = raw.size - (raw.size % 3)
        if usable <= 0:
            return np_mod.empty(0, dtype=np_mod.float32)
        triplets = raw[:usable].reshape(-1, 3).astype(np_mod.int32)
        values = triplets[:, 0] | (triplets[:, 1] << 8) | (triplets[:, 2] << 16)
        values = np_mod.where(values >= 1 << 23, values - (1 << 24), values)
        return values.astype(np_mod.float32) / 8388608.0
    if width == 4:
        return np_mod.frombuffer(frames, dtype='<i4').astype(np_mod.float32) / 2147483648.0
    raise ValueError(f'不支持的 WAV 采样位宽：{width * 8} bit')


def _resample(samples, source_rate: int, target_rate: int = SAMPLE_RATE):
    np_mod = _numpy()
    if source_rate == target_rate or samples.size == 0:
        return samples
    target_size = max(1, int(round(samples.size * float(target_rate) / source_rate)))
    positions = np_mod.linspace(0.0, samples.size - 1, target_size, dtype=np_mod.float64)
    source_positions = np_mod.arange(samples.size, dtype=np_mod.float64)
    return np_mod.interp(positions, source_positions, samples).astype(np_mod.float32)


def _decode_wav(path: Path):
    """标准库 wave 兜底：任意 PCM 位宽/声道/采样率 -> float32 mono 16 kHz。"""
    np_mod = _numpy()
    try:
        with wave.open(str(path), 'rb') as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError) as exc:
        raise AsrUnavailable(f'WAV 解码失败：{path.name}（{exc}）') from exc
    if not frames:
        raise AsrUnavailable(f'WAV 文件没有音频数据：{path.name}')
    samples = _pcm_to_float(frames, width)
    if channels > 1:
        usable = samples.size - (samples.size % channels)
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    samples = _resample(samples, rate)
    return np_mod.ascontiguousarray(samples, dtype=np_mod.float32)


def _clean_text(text) -> str:
    """清洗一句转写：折叠空白/重复标点，去掉 whisper 幻听串，空内容返回空串。"""
    value = ' '.join(str(text or '').split())
    if not value:
        return ''
    if HALLUCINATION_RE.search(value):
        value = HALLUCINATION_RE.sub(' ', value)
    value = ' '.join(value.split())
    value = PUNCTUATION_RUN.sub(r'\1', value)
    value = value.strip().lstrip(EDGE_PUNCTUATION)
    if not value or not CONTENT_CHAR.search(value):
        return ''
    return value


def _as_samples(pcm):
    np_mod = _numpy()
    if pcm is None:
        raise ValueError('PCM 数据不能为 None')
    try:
        samples = np_mod.asarray(pcm, dtype=np_mod.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError('PCM 数据必须是可转换为 float32 的采样序列') from exc
    samples = np_mod.ravel(samples)
    if samples.size == 0:
        return samples
    return np_mod.ascontiguousarray(samples, dtype=np_mod.float32)


def _confidence(raw) -> float | None:
    value = getattr(raw, 'avg_logprob', None)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(max(0.0, min(1.0, math.exp(number))), 4)


class Transcriber:
    """整段转写器：懒加载 faster-whisper 模型。

    失败模式：引擎未安装、模型未下载、模型加载或推理异常均抛 AsrUnavailable；
    PCM 非法抛 ValueError。
    """

    def __init__(self, name: str = 'turbo', *, compute_type: str = 'int8',
                 device: str = 'cpu', model_dir=None, prompt_terms=None):
        _spec(name)
        self.name = str(name).strip()
        self.compute_type = str(compute_type)
        self.device = str(device)
        self.model_dir = Path(str(model_dir)).expanduser() if model_dir else None
        terms = [str(term).strip() for term in (prompt_terms or []) if str(term).strip()]
        self.prompt_terms = tuple(terms)
        self._model = None

    @property
    def directory(self) -> Path:
        return self.model_dir or models_dir(self.name)

    def load(self):
        """导入引擎并加载模型（只加载一次）。"""
        if self._model is not None:
            return self._model
        np_mod = _numpy()
        module = _import_whisper()
        directory = self.directory
        if not _file_ready(directory / 'model.bin'):
            raise AsrUnavailable(
                f'语音模型尚未下载：{directory}。请先调用 download_model("{self.name}")。')
        try:
            self._model = module.WhisperModel(str(directory), device=self.device,
                                              compute_type=self.compute_type)
        except Exception as exc:
            raise AsrUnavailable(f'语音模型加载失败（{directory.name}）：{exc}') from exc
        LOGGER.info('ASR model loaded name=%s dir=%s device=%s compute_type=%s',
                    self.name, directory, self.device, self.compute_type)
        return self._model

    def _prompt(self, initial_prompt) -> str:
        text = DEFAULT_PROMPT if initial_prompt is None else str(initial_prompt).strip()
        if self.prompt_terms:
            terms = '、'.join(self.prompt_terms)
            text = f'{text}术语：{terms}。' if text else f'术语：{terms}。'
        return text[:MAX_PROMPT_CHARS]

    def transcribe(self, pcm, *, language: str = 'zh', initial_prompt=None,
                   beam_size: int = 1, vad_filter: bool = False):
        """整段 PCM -> (list[Segment], 音频时长秒)。Segment.start/end 单位为秒。"""
        samples = _as_samples(pcm)
        duration = 0.0 if samples.size == 0 else samples.size / float(SAMPLE_RATE)
        if samples.size == 0:
            return [], duration
        model = self.load()
        prompt = self._prompt(initial_prompt)
        try:
            raw_segments, info = model.transcribe(
                samples, language=language, initial_prompt=prompt or None,
                beam_size=beam_size, vad_filter=vad_filter)
            segments = []
            for raw in raw_segments:
                text = _clean_text(getattr(raw, 'text', ''))
                if not text:
                    continue
                start = float(getattr(raw, 'start', 0.0) or 0.0)
                end = float(getattr(raw, 'end', start) or start)
                segments.append(Segment(start=start, end=max(end, start),
                                       text=text, confidence=_confidence(raw)))
        except AsrUnavailable:
            raise
        except Exception as exc:
            raise AsrUnavailable(f'语音识别失败：{exc}') from exc
        segments.sort(key=lambda item: item.start)
        try:
            total = float(getattr(info, 'duration', duration))
        except (TypeError, ValueError):
            total = duration
        return segments, (total if total > 0 else duration)


def _empty_samples():
    np_mod = _numpy()
    return np_mod.empty(0, dtype=np_mod.float32)


def _concat_samples(first, second):
    np_mod = _numpy()
    if first.size == 0:
        return second
    if second.size == 0:
        return first
    return np_mod.concatenate((first, second)).astype(np_mod.float32, copy=False)


def _chunk_samples(pcm_chunk):
    """把输入块转成 float32 一维数组；无法转换时返回 None（丢包不崩）。"""
    if pcm_chunk is None:
        return None
    try:
        np_mod = _numpy()
        samples = np_mod.asarray(pcm_chunk, dtype=np_mod.float32)
    except AsrUnavailable:
        raise
    except (TypeError, ValueError):
        LOGGER.warning('ASR stream ignored a non-numeric PCM chunk')
        return None
    samples = np_mod.ravel(samples)
    return np_mod.ascontiguousarray(samples, dtype=np_mod.float32)


def _trim_overlap(previous: str, text: str, min_chars: int = MIN_OVERLAP_CHARS) -> str:
    """去掉新文本开头与上一段结尾重复的 >=2 个字。"""
    if not previous or not text:
        return text
    limit = min(len(previous), len(text))
    for size in range(limit, min_chars - 1, -1):
        if previous.endswith(text[:size]):
            return text[size:]
    return text


class StreamSession:
    """增量转写会话：边喂 PCM 边出片段。

    buffer 填满 window_s 秒后转写，再滚动 hop_s 秒（window-hop 为重叠区）。
    跨窗口按字符级后缀-前缀重叠（>=2 字）截断，避免同一句重复上屏；返回的
    Segment.start/end 是会话绝对毫秒（int）。feed 对 None/空/非数值块容错忽略；
    引擎或推理失败抛 AsrUnavailable，保留尚未返回的音频供重试或收尾。
    """

    def __init__(self, name: str = 'turbo', *, transcriber=None,
                 window_s: float = DEFAULT_WINDOW_SECONDS,
                 hop_s: float = DEFAULT_HOP_SECONDS,
                 sample_rate: int = SAMPLE_RATE, language: str = 'zh',
                 initial_prompt=None, prompt_terms=None, beam_size: int = 1,
                 vad_filter: bool = False, min_flush_s: float = MIN_FLUSH_SECONDS):
        _spec(name)
        if window_s <= 0:
            raise ValueError('window_s 必须大于 0')
        if hop_s <= 0 or hop_s > window_s:
            raise ValueError('hop_s 必须大于 0 且不大于 window_s')
        if sample_rate <= 0:
            raise ValueError('sample_rate 必须大于 0')
        self.name = str(name).strip()
        self.sample_rate = int(sample_rate)
        self.window_samples = max(1, int(round(float(window_s) * self.sample_rate)))
        self.hop_samples = max(1, int(round(float(hop_s) * self.sample_rate)))
        # Retained for constructor compatibility; final flush never drops audio.
        self.min_flush_samples = max(0, int(round(float(min_flush_s) * self.sample_rate)))
        self.language = language
        self.initial_prompt = initial_prompt
        self.beam_size = int(beam_size)
        self.vad_filter = bool(vad_filter)
        self._transcriber = transcriber if transcriber is not None else Transcriber(
            self.name, prompt_terms=prompt_terms)
        self.reset()

    def reset(self) -> None:
        """丢弃当前缓冲与去重状态，开始新一段旁听。"""
        self._buffer = _empty_samples()
        self._consumed = 0
        self._tail = ''
        self._last_end = 0.0

    def set_terms(self, prompt_terms) -> None:
        """更新热词（下一次转写窗口生效）。"""
        transcriber = self._transcriber
        terms = [str(term).strip() for term in (prompt_terms or []) if str(term).strip()]
        if hasattr(transcriber, 'prompt_terms'):
            transcriber.prompt_terms = tuple(terms)

    def _recognize(self, window, base_ms: float):
        segments, _duration = self._transcriber.transcribe(
            window, language=self.language, initial_prompt=self.initial_prompt,
            beam_size=self.beam_size, vad_filter=self.vad_filter)
        emitted = []
        tail, last_end = self._tail, self._last_end
        for segment in segments or []:
            text = _clean_text(getattr(segment, 'text', ''))
            if not text:
                continue
            text = _trim_overlap(tail, text).lstrip(EDGE_PUNCTUATION)
            if not text:
                continue
            start = int(round(base_ms + float(getattr(segment, 'start', 0.0) or 0.0) * 1000.0))
            end = int(round(base_ms + float(getattr(segment, 'end', 0.0) or 0.0) * 1000.0))
            start = max(start, int(round(last_end)))
            end = max(end, start)
            emitted.append(Segment(start=start, end=end, text=text,
                                   confidence=getattr(segment, 'confidence', None)))
            tail = (tail + text)[-TAIL_CHARS:]
            last_end = end
        self._tail, self._last_end = tail, last_end
        return emitted

    def feed(self, pcm_chunk):
        """追加一块 PCM（float32 单声道 16 kHz），返回新产生的片段列表。"""
        samples = _chunk_samples(pcm_chunk)
        if samples is None or samples.size == 0:
            return []
        self._buffer = _concat_samples(self._buffer, samples)
        checkpoint = (self._buffer, self._consumed, self._tail, self._last_end)
        emitted = []
        try:
            while self._buffer.size >= self.window_samples:
                window = self._buffer[:self.window_samples]
                base_ms = self._consumed * 1000.0 / self.sample_rate
                emitted.extend(self._recognize(window, base_ms))
                self._consumed += self.hop_samples
                self._buffer = self._buffer[self.hop_samples:]
        except Exception:
            self._buffer, self._consumed, self._tail, self._last_end = checkpoint
            raise
        emitted.sort(key=lambda item: item.start)
        return emitted

    def flush(self):
        """转写所有非空最终尾段；成功后才清空缓冲和推进时间。"""
        if self._buffer.size == 0:
            return []
        window = self._buffer
        base_ms = self._consumed * 1000.0 / self.sample_rate
        emitted = self._recognize(window, base_ms)
        self._consumed += int(window.size)
        self._buffer = _empty_samples()
        emitted.sort(key=lambda item: item.start)
        return emitted
