"""Pinned, offline-only BGE evaluation adapter; not a production chat provider."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.parse
import urllib.request

MANIFEST = Path(__file__).resolve().parents[1] / 'docs/examples/bge-small-zh.model.json'


def manifest():
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def verified(path, entry):
    if not path.is_file() or path.is_symlink() or path.stat().st_size != entry['bytes']:
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == entry['sha256']


def download(destination):
    """Explicit CLI operation; inference never calls this function."""
    spec = manifest()
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for name, entry in spec['files'].items():
        target = destination / name
        if target.exists() or target.is_symlink():
            if not verified(target, entry):
                raise ValueError('Existing model file does not match pinned manifest')
            continue
        query = urllib.parse.urlencode({'Revision': spec['revision'], 'FilePath': name})
        url = f"https://modelscope.cn/api/v1/models/{spec['repository']}/repo?{query}"
        fd, temporary = tempfile.mkstemp(prefix='.download-', dir=destination)
        started, size = time.monotonic(), 0
        try:
            with os.fdopen(fd, 'wb') as output, urllib.request.urlopen(url, timeout=30) as response:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > entry['bytes'] or time.monotonic() - started > 600:
                        raise ValueError('Model download exceeded declared size/time')
                    output.write(chunk)
            if not verified(Path(temporary), entry):
                raise ValueError('Model checksum mismatch')
            os.replace(temporary, target)
            print(f'{name}: verified {size} bytes', flush=True)
        finally:
            Path(temporary).unlink(missing_ok=True)


class OfflineEmbedding:
    def __init__(self, directory):
        spec = manifest()
        directory = Path(directory).expanduser().resolve()
        if not all(verified(directory / name, entry) for name, entry in spec['files'].items()):
            raise ValueError('Pinned model files are missing or invalid; no automatic download')
        from fastembed import TextEmbedding
        version = importlib.metadata.version('fastembed')
        if version != '0.9.0':
            raise ValueError('Evaluation requires fastembed 0.9.0')
        self.model = spec['model']
        runtimes = {p: importlib.metadata.version(p) for p in ('fastembed', 'onnxruntime', 'tokenizers', 'numpy')}
        contract = {'files': spec['files'], 'runtimes': runtimes, 'max_text': 6000,
                    'pooling': 'CLS normalized', 'query_prefix': '', 'max_tokens': 512}
        self.identity = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
        self.details = {'model': self.model, 'model_sha256': spec['files']['model_optimized.onnx']['sha256'],
                        'revision': spec['revision'], 'fastembed': version, 'runtimes': runtimes,
                        'provider': 'CPUExecutionProvider', 'dimension': 512, 'max_tokens': 512,
                        'query_prefix': '', 'files_bytes': sum(f['bytes'] for f in spec['files'].values())}
        self.engine = TextEmbedding(model_name=self.model, specific_model_path=str(directory),
                                    local_files_only=True, threads=2, providers=['CPUExecutionProvider'])

    def embed(self, texts):
        return [v.tolist() for v in self.engine.embed([t[:6000] for t in texts], batch_size=16)]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', type=Path, required=True, help='Explicit destination outside share packages')
    args = parser.parse_args()
    download(args.download)
