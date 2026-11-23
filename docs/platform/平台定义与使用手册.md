# MinDock

MinDock turns your local notes, documents, and Obsidian Wiki into a searchable personal knowledge base. Use StudyPal, WorkPal, or LifePal to organize material by context, follow connections in the knowledge graph, and ask questions with links back to evidence. You can review AI-extracted candidates before adding them to your graph.

思泊（MinDock）是一个本地优先的个人知识工具。它支持导入资料、检索原文、浏览知识图谱，以及连接你选择的模型进行问答。

Source version: see [VERSION](VERSION).

MinDock source code is available under the [MIT License](LICENSE). Bundled
third-party components retain their respective licenses. See
[Third-party notices](THIRD-PARTY-NOTICES.md).

## Install And Start

Requires Python 3.11+ (Python 3.13 recommended for development). Node.js is
needed for JavaScript tests and Windows desktop builds, not browser runtime.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8899
```

On Windows use `.venv\Scripts\python.exe` instead of `.venv/bin/python`.
Alternatively run `启动伴学.command` on macOS or `start_cyberpal.bat` on Windows.
The launchers install the base dependencies on first use. Follow the local URL
printed at startup. Do not expose the local service to the public Internet.

Your runtime data is created locally in `data/`; it is not part of this source
distribution. Public sample data lives in `seed/public/` (also mirrored in
`seed/` for compatibility). It is demo content, not your private course library.
To load the public samples explicitly:

```sh
.venv/bin/python scripts/load_memory.py
```

## Usage

### 课程材料上传与存储

Create or select a course, then upload your own material in its capture panel.
Wait for indexing, review extracted candidates, and confirm the items you want
in the graph. Original files, conversations and notes stay in your local data
directory. Back up that directory before upgrades or imports.

Select the correct workspace and course before searching. Empty results do not
mean that an unindexed attachment or private Wiki was bundled with the app.
Wiki import requires an explicitly selected local directory; no private Wiki
content is distributed here. Review sources before accepting generated text.

### Models And MCP

Configure your own model endpoint and credentials in Settings. Without a model,
local records and search remain available, but model-generated answers are not.
Do not put credentials in source files, commands, screenshots or committed JSON.

For MCP, add a trusted, separately installed server, select its scope, use
“连接并发现工具”, then select read-only tools and save. MCP is optional and does not
install or sandbox third-party programs. Credentials should be referenced by
environment variable names. See `docs/examples/mcp.example.json` (disabled by
default) and `docs/platform/本地扩展与向量检索.md`.

### Optional Features And Limits

```sh
.venv/bin/python -m pip install -r requirements-extensions.txt
.venv/bin/python scripts/local_extensions.py status
```

OCR, speech recognition and local vector experiments need optional dependencies
and, where applicable, separately downloaded models or OS permissions. Do not
commit model files or recordings. Speech chunk receipts and session locks are
process-local; unpersisted audio is not recoverable after a process crash.
An accepted chunk receipt is not a durable audio backup.

Network tools and remote model endpoints can send selected content to their
providers. Disable them when working offline or with restricted material.
Email, mobile notifications and production multi-user hosting are not provided.
Vector experiment tooling is not a guarantee of answer correctness.

## Development And Tests

```sh
.venv/bin/python -m pip install -r requirements-dev.txt -r requirements-extensions.txt
npm ci
```

Use an empty temporary data directory, never your personal data directory:

```sh
CYBERPAL_DATA_DIR="$(mktemp -d)" .venv/bin/python -m pytest -q tests
npm run test:node
node --test desktop/windows/tests/*.test.cjs
.venv/bin/python -m compileall -q app scripts desktop
PYTHON=.venv/bin/python bash scripts/check_licenses.sh
```

On PowerShell set `$env:CYBERPAL_DATA_DIR` to a new temporary directory before
running tests. Optional real-model tests are opt-in. Do not use private Wiki or
textbook files as public fixtures. The test fixtures are synthetic examples.
Retrieval evaluation dependencies are in `requirements-retrieval-eval.txt`.

## Desktop Builds

macOS builds require macOS, Xcode command-line tools and the build dependencies:

```sh
.venv/bin/python -m pip install -r desktop/requirements-build.txt
.venv/bin/python scripts/build_macos.py --help
```

Windows builds use Electron, official Python runtime downloads and pinned wheels:

```sh
npm ci --prefix desktop/windows
.venv/bin/python scripts/build_windows.py --help
```

Build scripts place artifacts under `output/`. See each command's options and
the pinned desktop requirement files. Optional component checks are performed by
`scripts/check_extension_packaging.py`. Cross-building and automated tests do not
replace native installation, microphone/OCR, upgrade or uninstall acceptance on
the target OS. Signing, notarization and Windows native acceptance are separate
release steps; do not claim a source export passed them.

## License

See `LICENSE` for this project's terms and `THIRD-PARTY-NOTICES.md` for dependency
notices. Vendored JavaScript licenses are retained beside their source files in
`web/assets/` and `web/assets/vendor/`. Source availability does not change those
license terms.
