<!-- PUBLIC-README:START -->
# MinDock 思泊 · 个人 360 记忆库

**让思考有所停泊，让成长自有航向**

MinDock 思泊是一个本地优先的个人 360 记忆库。它把学习、工作和生活中的资料、笔记、对话与经验积累在一起。遇到新问题时，你可以找回相关记录，沿着知识图谱查看它们的联系，再回到原文核对出处。

- **StudyPal 学习**：归档教材和课程笔记，整理知识点，记录掌握进度。
- **WorkPal 工作**：积累项目记录与行业知识，接入已有的 Obsidian Wiki。
- **LifePal 生活**：记录生活计划、待办和日常经验。

接入自己的模型后，可以围绕库内资料提问，也可以让 AI 提议知识点和关系，审阅后再加入图谱。

## 本地安装与启动

需要 Python 3.11 或更新版本，开发推荐 Python 3.13。Node.js 用于前端测试和 Windows 桌面构建。

```
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8899
```

Windows 下用 `.venv\Scripts\python.exe` 替换 `.venv/bin/python`。也可以在 macOS 双击 `启动伴学.command`，或在 Windows 运行 `start_cyberpal.bat`。启动脚本会在首次运行时安装基础依赖。服务就绪后，用本机浏览器打开 [MinDock](http://127.0.0.1:8899/)。

首次运行会在本机创建 `data/`。公开示例位于 `seed/public/`，可用以下命令加载。升级或批量导入前，请先备份数据目录。

```
.venv/bin/python scripts/load_memory.py
```

## 开始使用

### 课程材料上传与存储

在学习工作区选择或新建课程，从录入页添加资料。索引完成后可以搜索原文、检查提取的知识候选，并确认要加入图谱的条目。搜索前先选定工作区和课程。导入 Obsidian Wiki 时，在应用内选择本机库目录。

### 模型、联网与 MCP

在「设置」中配置自己的模型接口和密钥。记录、浏览与本地检索可以独立使用，AI 问答和知识抽取需要可用的模型接口。使用联网工具或远程模型时，选中的内容可能会发送给对应服务商。

使用 MCP 时，先安装可信的服务，再在应用中限定使用范围，点击「连接并发现工具」，选好只读工具后保存。可以用环境变量引用凭据。配置和扩展说明见仓库内的 `docs/examples/mcp.example.json` 和 `docs/platform/本地扩展与向量检索.md`。

### OCR、录音与向量检索

```
.venv/bin/python -m pip install -r requirements-extensions.txt
.venv/bin/python scripts/local_extensions.py status
```

OCR、语音识别和向量检索可能需要额外的模型或系统权限。录音完成后请确认已保存，进程退出后可能无法恢复尚未落盘的音频。

## 开发与测试

```
.venv/bin/python -m pip install -r requirements-dev.txt -r requirements-extensions.txt
npm ci
CYBERPAL_DATA_DIR="$(mktemp -d)" .venv/bin/python -m pytest -q tests
npm run test:node
node --test desktop/windows/tests/*.test.cjs
.venv/bin/python -m compileall -q app scripts desktop
PYTHON=.venv/bin/python bash scripts/check_licenses.sh
```

测试使用临时数据目录。Windows PowerShell 中请先将 `$env:CYBERPAL_DATA_DIR` 设为新的临时目录。真实模型测试按需启用，检索评测依赖见 `requirements-retrieval-eval.txt`。

## 构建桌面版

macOS 构建需要 Xcode 命令行工具和桌面构建依赖。

```
.venv/bin/python -m pip install -r desktop/requirements-build.txt
.venv/bin/python scripts/build_macos.py --help
```

Windows 构建使用 Electron、官方 Python 运行时和固定版本的依赖。

```
npm ci --prefix desktop/windows
.venv/bin/python scripts/build_windows.py --help
```

构建产物位于 `output/`。安装、OCR、麦克风权限、升级和卸载以目标系统实机验证为准。

## 版本与许可证

当前源码版本见 `VERSION`，版本变更见 `CHANGELOG.md`。MinDock 自有源码采用 MIT 协议，所包含的第三方组件保留各自的许可证。详见 [MIT 许可证](https://github.com/ramoncooling-hue/airamon/blob/main/LICENSE) 和 [第三方组件声明](https://github.com/ramoncooling-hue/airamon/blob/main/THIRD-PARTY-NOTICES.md)。
<!-- PUBLIC-README:END -->

## 更新 GitHub 公开源码

公开版的 README 和平台使用手册由上方内容生成。导出脚本根据清单复制源码、测试、演示数据和桌面构建所需文件，并扫描凭据、私钥和本机用户路径。个人数据库、教材、私人 Wiki、品牌长图和开发记录留在本机。

```
python3 scripts/prepare_github_source.py \
  --output output/github/MinDock-0.6.4 \
  --report output/github/MinDock-0.6.4-report.json
```

输出目录和报告需使用新路径。每个公开文件的路径、大小、权限和 SHA-256 记录在 `GITHUB-SOURCE-MANIFEST.json` 中。导出后，在临时数据目录测试公开源码，再把导出目录里的文件和文件夹提交到 GitHub。扫描失败时先修复报告所列的源文件，再重新导出。
