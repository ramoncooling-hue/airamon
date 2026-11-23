<!-- PUBLIC-README:START -->
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
<!-- PUBLIC-README:END -->

## Preparing A New GitHub Tree

The exporter copies only allowlisted source/configuration, all current tests and
reviewed fixtures, public seed, desktop build inputs, launchers, vendor code and
licenses, and the installation/usage documents listed in the script. It reuses
the exclusions and desktop whitelist in `scripts/zip_share_utf8.py`, but does
not run the existing share builder or copy its broad docs selection.

`README.md` and the bundled platform usage manual are generated from the public
section above. A minimal public `DEMO-页面索引.md` is generated for compatibility
with the unchanged share builder; `CHANGELOG.md` is copied and scanned. The
original development README is never read. Design rationale,
brand manuals/long images, internal handoffs, historical reports, private Wiki,
textbooks, `.git/`, `.env*`, local settings, databases, models, caches, installed
dependencies and previous outputs are not copied. Runtime application icons and
synthetic test fixtures remain because the program and tests require them.

```sh
python3 scripts/prepare_github_source.py \
  --output output/github/MinDock-0.6.4 \
  --report output/github/MinDock-0.6.4-report.json
```

Both paths must be new. Existing exports/reports are never overwritten or
deleted; choose a new suffix for later snapshots. The exporter never runs Git,
initializes a repository, modifies history, reads personal `data/`, starts a
service or deletes old artifacts. Upload the *contents of the new exported
directory*, not the original development checkout or its Git history. Gitignore
is defense in depth, not a substitute for the export and manual review.

### Reports And Failure Handling

`GITHUB-SOURCE-MANIFEST.json` lists every exported payload file with its relative
path, SHA-256, byte size and normalized mode. The report excludes its own hash.
Files, ordering, modes and timestamps are stable for unchanged input bytes.
The optional external JSON report contains the same information on success.

Privacy scanning checks allowlisted file bytes for credential literals, common
provider tokens, private keys, URL credentials and personal home paths. Failures
return status 2 and do not publish the requested tree. Reports contain rule,
relative path and line only, never the matched secret or source excerpt. If a
source file contains a private path, fix the source through its owner, then
rerun; the exporter does not silently rewrite program behavior. Synthetic test
credentials require an explicitly reviewed, exact file SHA in the script; a
changed file is no longer exempt. No directory-wide test exemption is used.

The scanner is a conservative release check, not proof that every possible
secret or copyrighted passage has been detected. Review the full manifest and
the public seed/fixtures before upload. Unrecognized source files require an
explicit policy review; private paths are not traversed to produce a listing.

### Final Release Gate

Wait for all source workers to finish before the final export. Freeze source
changes, check `VERSION` and Windows package/lock versions agree, export to a
new directory, and run Python/Node tests from that directory with temporary data.
Check dependency/license tooling and desktop build inputs there as well. This
script's successful scan does not mean build/tests or native acceptance ran.
The original `scripts/build_share_zip.sh` remains unchanged and is not a
replacement for this stricter public-directory policy.
