# MinDock Local Package

Source distribution for running MinDock locally on macOS or Windows.

## Requirements

- Python 3.11 or later
- A language-model API configuration for model-backed responses (optional for initial local setup)

## Install

1. Extract the release ZIP to a writable folder.
2. On macOS, run `启动伴学.command`.
3. On Windows, run `start_cyberpal.bat`.
4. Open the local address shown by the launcher. The default service binds to `127.0.0.1:8899`.

The launcher creates a virtual environment and installs the dependencies on first run. Runtime data, settings, and the SQLite database are stored locally and are not included in the release package. Configure any model credentials in the local application settings; do not commit them.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python scripts/verify_local_install.py --data-dir /tmp/mindock-verify
python -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.cjs
```

On Windows, activate the virtual environment with `.venv\\Scripts\\activate`.

The source includes application code, schemas, public seed data, Agent/Skill templates, the in-app operating manuals, and tests. User databases, API keys, settings, uploaded course materials, brand/presentation material, and conversation history are excluded.

## License

PolyForm Noncommercial License 1.0.0. Noncommercial use, modification, and redistribution are allowed under the license terms. Commercial use requires separate permission. See `LICENSE`.
