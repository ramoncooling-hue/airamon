"""Model-free Windows ASR probe; generated audio stays in a temporary directory."""

import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import wave


def probe(backend=None):
    backend = Path(backend or Path(__file__).resolve().parents[2])
    sys.path.insert(0, str(backend))
    ffmpeg = backend / 'bin/ffmpeg.exe'
    with tempfile.TemporaryDirectory(prefix='mindock-asr-probe-') as directory:
        root = Path(directory)
        os.environ['CYBERPAL_DATA_DIR'] = str(root)
        import numpy as np
        import ctranslate2
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import get_speech_timestamps
        from app.memory.asr import decode_audio_file

        wav = root / 'silence.wav'
        with wave.open(str(wav), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(bytes(16000 * 2))
        decoded = decode_audio(str(wav), sampling_rate=16000)
        assert decoded.shape == (16000,) and decoded.dtype == np.float32
        assert np.isfinite(decoded).all()
        assert get_speech_timestamps(decoded) == []
        assert decode_audio_file(wav).shape == (16000,)
        mp3 = root / 'silence.mp3'
        subprocess.run([str(ffmpeg), '-nostdin', '-loglevel', 'error', '-i', str(wav), str(mp3)],
                       check=True, capture_output=True, timeout=30)
        decoded_mp3 = decode_audio_file(mp3, ffmpeg=str(ffmpeg))
        assert decoded_mp3.size > 0 and np.isfinite(decoded_mp3).all()
        compute = sorted(ctranslate2.get_supported_compute_types('cpu'))
    return {'ok': True, 'versions': {name: importlib.metadata.version(name) for name in
            ('faster-whisper', 'ctranslate2', 'onnxruntime', 'numpy', 'av')},
            'checks': ['PyAV synthetic WAV decode', 'native Silero VAD silence',
                       'application WAV decode', 'bundled ffmpeg MP3 encode/application decode'],
            'cpu_compute_types': compute, 'speech_model_loaded': False,
            'scope': 'synthetic audio and VAD only; no speech accuracy or microphone acceptance'}


if __name__ == '__main__':
    print(json.dumps(probe(), indent=2))
