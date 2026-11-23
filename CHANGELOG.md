# Changelog

## 0.6.4 - 2026-10-23

- 桌面打包补齐 faster-whisper、VAD、音频解码与 PDF 页图依赖；固定兼容的 PyAV 16.1.0，并加入不下载模型、不读取个人文件的冻结运行探针。
- Mac 麦克风仅对已验证的本次本机后端来源请求系统授权；摄像头、外部来源与错误端口拒绝。系统声音采集并未接入原生 ScreenCaptureKit。
- 提供白名单 GitHub 源码导出：排除私人 Wiki、教材、运行库、模型配置、长图与内部设计材料；安装器独立作为 Release 资产。
- 过程输出按明确清单可恢复归档，个人数据、模型缓存和开发环境不移动。桌面验收与发布入口见 `docs/RELEASE-0.6.4.md`。

- 修复旁听收尾丢失超过20秒的录音尾段、在途上传竞态、失败后误 finalize、迟到麦克风授权以及旧会话删除响应误清理新会话的问题。
- 音频分片增加稳定 ID 与单进程幂等回执；确认已收取但 ASR 失败时不重复追加 PCM；finalize 重试恢复已有课堂记录入口。
- 修复并发 finalize 的文档/修订/学习事件重复，以及 ASR 最终短尾段被丢弃；写入/识别失败可按当前进程会话状态重试。
- Agent 动作执行器将明确业务失败记为 failed；030 数据库迁移支持 `save_memory_block` 且保留旧动作、租约、外键和序列。
- 恢复 026 已发布迁移原始字节，避免现有数据库因 checksum 不一致而拒绝启动。
- 全量验证：pytest 1278 passed、1 skipped、72 subtests；Node 372 passed；compileall、本地安装隔离演练、许可与 diff 检查通过。
- 上述稳定性回归属于源码验证；安装包、原生权限与真实音频验证分别记录，不互相替代。分片回执仅在单进程内有效，不承诺崩溃或多 worker exactly-once。

## 0.6.3 - 2026-10-22

用户反馈两件事：把本机 Obsidian Wiki 一键接进 WorkPal；左侧导航太散、录音/课程/知识这些录入入口能不能合并。本版新增「录入」页作为统一入口，并让 Wiki 成为默认可检索的知识来源。无新增数据库迁移；PyYAML 显式写入应用依赖（macOS/Windows 冻结锁已包含该包）。

### 一键接入 Obsidian Wiki
- 新增 `app/memory/wiki_import.py`（656 行，由原 CLI 脚本迁入并补 API）：读取 macOS/Windows/Linux 的 `obsidian.json` 发现 vault（优先含 `wiki/` 子目录的库），解析 frontmatter 与 `[[链接]]`，把页面登记为 `source_files`、主题为课程章节、可唯一解析的链接写成 `kp_edges`，证据落 `kp_evidence`；页面原文复制到 `data/WorkPal/<课程>/wiki/`。
- Wiki 页面使用 `source_kind='wiki_page'`：和教材、笔记同级参与默认检索（此前 `development_doc` 默认被排除，导入后在搜索和对话里看不到）；重复导入按页面 hash 只更新变化行，`ON CONFLICT` 会把 0.6.2 之前导入的旧行升级为新类型。
- 导入在后台单并发执行：`POST /api/wiki/import` 返回 202，`GET /api/wiki/import/status` 返回候选 vault 与 `idle/running/done/error`；坏路径或非法课程 400，已有任务运行中 409。导入前自动备份数据库，写入记 audit。
- `scripts/import_wiki_workpal.py` 收缩为 68 行薄 CLI，保留旧导出名与 `--apply`，供脚本/自动化继续使用。
- 前端：WorkPal 图谱页新增「导入 Wiki」按钮与对话框（候选 vault、记住上次目录、900ms 轮询进度、409 识别、10 分钟超时、文案全部转义）。

### Wiki 导入增强（2026-10-22）
- 支持自动识别 Obsidian `wiki/` 子目录、普通 Obsidian Vault 与手动指定 Markdown 目录；普通目录名叫 `wiki` 不会单凭目录名触发 LLM Wiki 的索引过滤。
- 源目录只读；扫描跳过隐藏/配置目录、输出目录及符号链接。YAML frontmatter 支持列表与别名；旧的不规范 YAML 尽量保留正文并在回执告警。
- 用 Wiki 相对路径作为知识点身份，重新导入可更新改名、摘要、正文和关系，同时保留熟练度；同名手工知识不合并，不同 Wiki 必须使用不同 WorkPal 主题。
- 双链解析忽略代码区，支持相对链接；唯一解析失败的目标在回执明细中展示，不虚构图谱节点。
- 导入完成后对话框保留新增/更新/关系/未解析/解析告警统计，并提供查看该 WorkPal 主题图谱入口。

### 录入中心
- 左侧导航新增「录入」页：旁听、材料、知识、Wiki 四张卡放在同一页，每张卡写明「选中后发生什么」，再进入对应流程；材料卡无课程时引导新建课程，知识卡直接打开新建对话框，Wiki 卡在工作区不匹配时提示切换。
- 旁听从左侧导航收起（导航 10 项 → 9 项），`#lectures` 深链保留；`go/hashchange/boot` 的页面回落统一走 `routeAllowed`（导航项 + `ROUTE_ONLY` 白名单 + 实际页面容器），避免深链被弹回。

### 导入验证与数据边界
- 用户 LLM Wiki 只读盘点：470 个成品页面、1,478 个双链目标、2 个 frontmatter 兼容告警。
- 隔离临时数据库端到端导入：470 页 / 470 节点 / 1,334 条关系 / 138 个未解析目标；重复导入新增节点/关系均为 0，搜索命中；SQLite integrity 与 foreign-key 检查通过。
- 本轮没有向正式用户数据库重复导入或覆盖 Wiki 文件；现有正式库保持原状。

### 测试与验证
- Wiki 专项：`tests/test_wiki_import.py` 25 例，`tests/wiki-import.test.cjs` 36 例。
- 全量门禁：`bash scripts/verify_all.sh` 全绿，最新正式目录结果见 `output/verify/20261022-215506`（pytest 1226 passed + 1 skipped + 60 subtests、Node 351 passed；含 compileall / 本地安装演练 / git-diff-check / 许可门禁）。
- 导入格式：支持 Obsidian vault 和普通 Markdown 目录；Wiki 页面与 `[[...]]` 双链可生成图谱关系。普通 Markdown 链接、非 Markdown 附件不会作为图谱关系或材料导入；其他系统需先导出 Markdown。
- 浏览器确认：正式源码在 `http://127.0.0.1:8899/#graph`，WorkPal 图谱可见 Wiki 导入入口、自动选中唯一 `wiki/` vault 候选；只打开对话框，未提交导入。

### 已知边界
- 整库同步不删除源库已删除的页面：源侧删页后本库节点仍在，需要手工归档（后续做对账）。
- 后台导入没有取消按钮，长库中途只能等本轮结束。
- 工作区暂不自动删除源 Wiki 已删除的页面，也没有后台任务取消按钮；删除对账和长任务取消留后续。
- 浏览器走查仅确认图谱入口和导入对话框；真实导入采用隔离临时库。录入中心完整交互、桌面原生窗口和安装包均未验收/重建。

## 0.6.2 - 2026-10-22

旁听转录 v0：上课时打开旁听页，本地引擎把麦克风/系统声音实时转成中文文字；停止后自动沉淀为带时间戳、带「旁听」标签的课堂记录，并写入学习区。只做中文转文字，不做翻译；音频不落库，只留文字；不需要模型 Key，全程本机（可选运行时依赖见 `requirements-extensions.txt`，未安装时功能引导安装、不影响主程序启动）。

### 旁听转录（029 / O-LEARN 延伸）
- 029 迁移：新增 `lecture_sessions`（工作区/课程/标题/来源 mic|system|upload/状态 live|ended|discarded/模型/起止时间/时长/生成文档/幂等键）与 `lecture_segments`（session_id 级联删除、seq、start_ms/end_ms、文本、置信度），4 个索引；幂等键用部分唯一索引（空串可重复）。音频本体不入库，v0 只存文字。
- 引擎层 `app/memory/asr.py`：faster-whisper 懒加载；模型文件走 hf-mirror 直链 HTTP 下载（绕开 huggingface_hub Xet 路径，墙内不会卡死）；统一解码为 16kHz 单声道 float32；`StreamSession` 用 6 秒窗口 / 4 秒跳步滚动转写，按字符后缀-前缀去重重叠，边录边出片段。默认模型 `small`（CPU 上 6 秒音频约 1-2 秒出字，约 480MB）；`turbo` 中文准确率更好，但 CPU 上转写速度约等于实时（6 秒音频 6-8 秒），定位为「适合导入音频或课后精校」。
- 会话层 `app/memory/lecture.py`：唯一写入口 `create_session / get_session / list_sessions / feed / finalize / discard`，进程内每会话独占一条转写流；`finalize` 用已落库片段生成 Markdown 课堂记录（标题「YYYY-MM-DD <课程> 旁听」，标签「旁听」，正文每段 `- **[HH:MM:SS]** 文本`，开头引用行标注日期/课程/时长/来源），同时写一条 `note_saved` 学习事件（`source=lecture`），学习区失败只记日志不打断落盘。
- 旁听 API：`GET /api/asr/status`、`POST /api/asr/models/{model}/download`、`POST /api/lectures`、`GET /api/lectures`、`GET /api/lectures/{id}`、`POST /api/lectures/{id}/chunks`（`application/octet-stream` 原始 PCM16）、`POST /api/lectures/{id}/finalize`、`DELETE /api/lectures/{id}`；引擎缺失 503、模型缺失/下载中 409、越界/未知课程 400。
- 前端「旁听」页：麦克风（`getUserMedia`）或系统声音（`getDisplayMedia`，需勾选分享标签页音频）开始；每 2 秒分片上传并滚动渲染实时字幕；停止先冲尾巴再 `finalize`，可直接打开生成的笔记；历史会话列表可回看与丢弃；顶部状态卡显示引擎/模型状态并提供下载。
- 稳健性：单次分片最多 20 秒（模型慢时避免积压把请求撑爆服务端 64 秒上限），余量留在缓冲区；引擎状态刷新同步更新左侧侧栏；所有字幕/标题/错误文案统一 `esc()`。

### 测试与验证
- pytest：`tests/test_lectures.py` 13 例（029 schema/索引/幂等、引擎与模型缺失降级、跨工作区隔离、推流与 finalize 建文档+学习事件、空会话不建文档、丢弃语义）；`tests/test_asr.py` 23 例（1 skipped：需真实引擎和模型文件）。
- Node：`tests/lectures-page.test.cjs` 21 例（状态卡、模型下载、实时字幕、历史、停止生成记录、默认 small、分片 20 秒上限、侧栏随状态刷新）。
- 门禁：`bash scripts/verify_all.sh` 全绿，见 `output/verify/20261022-124754`（pytest 1201 passed + 60 subtests、Node 299 passed；含 compileall / 本地安装演练 / git-diff-check / 许可门禁）。
- 实机：macOS M4 本地服务走通「开始麦克风 → 每 2 秒分片 → 实时字幕 → 停止 → 生成课堂记录 → 学习页事件落库」；headless 浏览器无法采集真实 WebAudio（用页面内 PCM 桩替代采集层），真实麦克风与系统声音采集仍需人工确认。

### 已知边界
- 不做翻译：只要中文实时文字记录；实时是「准实时」（首个字幕约 6-8 秒出现，之后每 4 秒一段）。
- 音频不留存，因此没有事后大模型重转（精校直接在生成的笔记里改）；small 模型偶有同音错字。
- 模型下载依赖 hf-mirror.com 可达；离线环境需手动把模型目录放到 `data/models/faster-whisper-<model>/`。
- 安装包未重建（模型不进包；用户首次在旁听页下载）。

## 0.6.1 - 2026-10-18

B3 批次：学习区闭环（读 → 记 → 学 → 练）+ 标签/导出 + 命令面板。核心是让「材料原文」和「学习动作」第一次成为一等公民：证据可点回原文片段、学习动作落成事件、复习有固定规则队列、知识可导出成 Obsidian vault。无新增运行时依赖。

### 学习闭环（O-LEARN-1..5 / 028）
- 028 迁移：新增 `learning_sessions`、`learning_events`、`quiz_attempts` 三表与 8 个索引，只用 `CREATE TABLE IF NOT EXISTS`，不改既有表、不进 FTS；幂等键用部分唯一索引（空串可重复），同一 key 重复提交只落一行。
- 学习事件单一写入口 `app/memory/learning.py`：`record_event`（幂等 + 自动补全课程/工作区）、会话开始/结束、`summary`（今日/本周/连续天数/最近材料）、`timeline`。
- 学习 API：`/api/learning/events`、`/sessions`（POST/PATCH）、`/summary`、`/timeline`。
- 材料原文（T3.5）：`/api/materials/{id}`、`/blocks`（按 locator 分页续读）、`/page.png`、`/api/evidence/{id}/context`；路径强制落在数据目录内，越界 400、文件缺失 404。页图三级降级：`pypdfium2`（桌面版有）→ pypdf 文本层 + Pillow 绘制 → 无文本层扫描页返回 415 + `fallback:'text'`，绝不 500、不落盘缓存。
- 材料阅读器 v1（D-16）：左页图 / 右文本块双栏，定位块高亮并滚动到视口，分页「加载更多」，选中/整块摘录成笔记（`POST /api/documents` kind=note、tags=['摘录']），页图不可用时自动退化为纯文本模式，失败可重试。
- 复习队列 v0（D-18）：固定规则（未读最高优先、3 天内不重复、熟练 14 天、考试 ≤7 天时该课高频提前），`GET /api/review/due`、`POST /api/review/feedback` 三档反馈（again→读过、hard→会用、good 只记事件），写回走 `repo.write_mastery(origin='review')` 留痕；纯查询不新增表。
- 学习页（D-17）：导航新增「学习」，今日学习卡（时长/事件/笔记/候选/复习/连续天数）、复习队列（三档反馈 + 队列结束页 + 考试倒计时）、按日期分组的学习时间线、最近材料一键回到阅读器。

### 知识与输出（O-KB-5..7）
- 标签体系：`/api/tags`、`/api/entity-tags`、`/api/tags/{id}/entities`，`GET /api/documents?tag=` 与 `GET /api/search?tag=` 可按标签筛选；标签作用域随实体所属工作区，软删实体拒绝挂标签；`list_tags` 返回 `count`/`entity_types` 并保留 `document_count` 兼容。
- 知识页标签筛选条（多选并集）、编辑页标签 chips + 按使用量排序的建议。
- Markdown 导出 v0（T3.11）：`POST /api/knowledge/export` 写出 `<data>/exports/knowledge-<stamp>/`，每文档一个 `.md`（YAML frontmatter，正文原样保留 `[[双链]]`，archived 进子目录，deleted 跳过并计数）+ `manifest.json`；文件名清洗非法字符与 `..`、重名自动 `-2/-3`、文件 0600；`dest_dir` 只接受数据目录内路径，越界 400。导出目录不参与备份/还原。

### 交互效率（D-09 / D-12）
- 命令面板 `Ctrl/Cmd+K`：导航 + 知识点 + 知识文档 + 命令四类来源（30s 缓存、每类 5 条），大小写不敏感高亮、方向键 + 回车、无结果一键「用对话回答」。
- 键盘与批量：`j/k` 移动、`x` 选择、`y` 确认、`n` 拒绝、`?` 帮助浮层、`Esc` 关闭；在输入框与浮层内自动失效。候选确认可在纯键盘下完成。

### 修复（本地试用反馈 2026-10-22）
- 图谱章节行错位：图谱数据里的 `chapter.title` 自带「第N章」前缀，前端又按 `number` 拼一次，导致「第1章 第1章 导论」。新增 `graphChapterTitle()` 先剥前缀再统一拼一次（含全角数字与「第三章」写法），树、章节节点、下一节按钮三处共用，源码里只保留一个格式化入口。
- 章节行布局：章节目录的标题、进度条、条数、下一节徽标原本挤在一行（207px 侧栏里标题被压成 54px 宽、折成三行）。进度部分收进 `.graph-chapter-meter` 单独占一行，标题 `nowrap + ellipsis` 单行省略并补 `title`/`aria-label` 全文。

### 测试与验证
- 新增 pytest 62 例（学习事件 16、复习队列 11、材料原文 16、标签 11、导出 8）、Node 56 例（阅读器 11、学习页 15、命令面板 16、标签与导出 14）。
- `verify_all.sh` 六步全绿：pytest 1165 passed + 60 subtests、Node 278 pass、compileall/verify-local-install/licenses/git diff check 通过（20261022-085129）。
- 022→028 旧库副本演练：真实 `data/app/banxue.db` 拷贝回滚到 026 后执行 `apply_schema()`，028 正常应用，`kp`/`courses`/`documents`/`evidence`/`source_files`/`tags`/`entity_tags` 行数不变。

### 已知边界（顺延）
- 页图渲染未在本机验证 `pypdfium2` 分支（未安装，桌面打包版才具备），当前走 pypdf 文本层绘制的降级路径。
- `/api/search?tag=` 只与 kp 命中求交（知识文档不在 FTS 索引），搜索面板暂不加标签下拉；文档级标签筛选走 `/api/documents?tag=`。
- 浏览器/桌面实机走查、安装包重建仍未做；024 编号（复习到期）保留给二期 SRS，027（Pod 边界）与 030（检索扩展）留 B5。

## 0.6.0 - 2026-10-17

B2 批次：数据可信 + 知识底座 v1。核心是「可编辑知识库」（Markdown 文档 + 双链 + 反链 + 修订），同时完成 M2 数据治理、工作流后台化与检索/材料链路加固；无新增运行时依赖。

### 知识底座 v1（O-KB-1..4 / 026）
- 026 迁移：新增 `knowledge_documents`、`document_revisions`、`tags`、`entity_tags`，`kp` 增加 `deleted_at`；旧 `kp` 自动回填 `concept` 文档；文档正文只经文档 API 写入，材料重扫不覆盖用户正文。
- 知识读写 API：`/api/documents`（建/改/软删/恢复/修订/回滚/反链/坏链/标题 lookup）、`/api/tags`、`/api/kp`（新建/软删）；PATCH 支持 `expected_version`，冲突返回 409。
- 知识页：Markdown 编辑+预览、标签、关联课程/知识点、修订历史与回滚、反链面板、坏链标记与新建、未保存离开提示；导航新增「知识」并在所有工作区可见。
- 双链 v1：`[[标题]]` / `[[标题|别名]]` 解析（忽略代码块），反链现算，坏链显式提示；B5/029 再引入 links 表。

### 数据与检索
- 023 迁移：把原 `db.py` legacy ALTER/索引/回填固化为编号迁移，新增 `schema_capabilities`；空库 schema 建库与旧库升级结构一致（drift 测试），旧库兼容分支保留。
- FTS5/trigram 能力探测：FTS5 缺失时跳过 011 并降级 LIKE，构建恢复后自动重放 011；FTS 失败按模块缺失/索引损坏/SQL 错误三类记日志。
- 检索：按 `(kind, ref_id)` 去重、超限带 `truncated` 标记；`development_doc` 材料默认不进入检索（`include_dev` 仅调试）；材料分块超上限显式标记。
- 读路径纯读：`GET /api/graph`、`/api/ingest/jobs`、`/api/bridges` 不再写库（`data_version` 不变），结构同步移到写事件；`/api/health` 增加 schema/迁移、FTS 能力、DB 大小、最近备份、工作区计数。
- 目录与队列：章节导航只认显式 active 目录（candidate 不导航）；ingest 心跳 5s/30s 收敛，无假 running；取消遇写锁返回 409 友好提示、索引完成后取消落 cancelled；DOCX 图片型材料明确 `ocr_required`；短文本 PDF 不再误判扫描件；页进度保留。
- 稳定身份：改名事务内重算 `node_key` 并回填 bridge 关系，导出/导入往返一致；重名/并发冲突明确报错。

### 工作流与 Agent
- 工作流后台化：启动/审批/重试立即返回 run 快照，客户端轮询 `status/current_step/progress`；重启遗留 running 置可重试 error，`waiting_approval` 保留。
- `/ask` 上下文注入去重并带 truncated 汇总，`search_memory` 工具复用同一去重语义。
- 正式写入通道：图谱导入/维护脚本走白名单 + `ingest_runs` 审核行，非法调用拒绝。

### 产品与可观测
- 候选确认台（D-03）：多选/全选、浮动批量栏、确认预览、5 秒延迟撤销、拒绝原因；失败保留选中。
- 对话引用侧栏（D-08 v1）：本地/网页/手册分组，定位图谱、查看片段、存为笔记、复制/继续追问；无引用明确标注「未引用本地资料」。
- 数据与隐私面板（D-10）：数据根 basename、schema/能力、备份、脱敏错误摘要、工作区计数，可导出脱敏诊断包（`/api/diagnostics`）。
- 图谱学习路径（D-07）：章节进度/下一节、节点动作、可选学习路径高亮；只读一致性巡检 CLI `scripts/check_graph_health.py`。
- 测试：较 0.5.7 +255 pytest、+57 Node；`verify_all.sh` 六步全绿（pytest 1093 passed + 60 subtests、Node 215 pass、licenses/diff check 通过）。独立复核发现的 P1（软删 kp 未在检索/邻居/写路径过滤）已修复并补 9 项回归。

### 本版未包含（按批次规则顺延，详见交接单与执行日志）
- T2.19 组件大版本升级（8-10d）、大文件索引吞吐与检索组合索引、027 Pod 数据边界、030 检索扩展顺延 B3/B5。
- T2.12 组织行为学原 PDF 导入仍 blocked-on-user；T2.17「生成知识候选」与材料阅读器原文定位、T2.13「加入复习」入口分别留 B3/B4。
## 0.5.7 - 2026-10-17

B1.1 加固批次：关闭 B1 独立复核提出的 7 个 P2（不阻塞 B1，但影响长期正确性），并收紧安全头。无新增 schema、无新依赖。

- 审批租约：执行结果按 attempt 栅栏写入，租约过期后被新 attempt 取代的旧结果只记日志、不覆盖当前状态；启动恢复在置 failed 时同步 attempt+1，崩溃进程无法把 failed 翻回 applied；failed 且为「租约超时」的动作可重新确认重试，其它失败不可自动重试。
- 会话回执：`/agent/ask` 响应拆出 `user_saved`/`assistant_saved`（`persisted` 语义不变），前端只补写服务端缺的那条消息，避免 assistant 落库失败时重复 user 行；流式回执同样生效。
- 重扫一致性：用户可见证据读路径（知识点详情/图谱 source_refs/关系候选/合同校验/关系建议/工作空间图谱）统一过滤 stale；重扫后消失标题的 pending 候选标记 superseded 并留审核记录（仅作用于扫描派生候选，AI 辅助拆录的概念候选不受影响），候选/确认/审核接口拒绝失效候选；标题回归时复活保留行；确认候选不再回退挂 stale 证据。
- 日志脱敏：覆盖 settings.json 中的模型 Key（fast/reasoning/web/model_profiles）与 URL 查询参数 secret，设置保存后立即刷新；轮转后日志文件保持 0600。
- 安全头：CSP 增加 `object-src 'none'`、`base-uri 'none'`、`frame-ancestors 'none'`、`form-action 'self'`；移除被静态挂载遮蔽的死路由。
- 测试：新增 SSE 端点级断连、线程级并发 approve、无 Origin 表单、日志脱敏、ask 回执、stale 可见性等回归；838 pytest + 60 subtests、158 Node 全绿。

## 0.5.6 - 2026-10-17

B1 加固批次：把"本机单用户"从口头假设变成可验证边界，并把 Personal Agent 的记忆与会话链路接通。

- 安全边界：`/api` 写接口统一做 Host/Origin 同源校验，CORS 默认只放行 loopback 源（`MINDOCK_DEV_CORS=1` 仅调试放开）；桌面壳启动时用一次性 ready nonce 校验后端身份；前端补 CSP/安全响应头，模型输出与搜索结果统一转义。
- 可靠性：审批动作改为单事务租约 + 崩溃恢复，并发重复确认不会重复执行；材料重扫改为快照软删，保留证据与候选历史；`/ask` 设 240 秒总预算（`MINDOCK_ASK_BUDGET_SECONDS` 可覆盖），模型重试共享截止时间，SSE 断开取消后端任务。
- 可观测：新增文件日志（自动脱敏）与 `/api/health` 指标（扫描时间、触发统计、动作计数、识别队列）。
- 记忆与会话：助教回答注入用户画像、分层记忆和当前会话窗口，`memory_policy=off` 可整块关闭；`/ask` 与流式接口支持 `conversation_id` 续接会话并把问答落库，响应回传 `persisted` 供前端去重。
- 产品 P0：首页今日驾驶舱、投放收件箱、五步流程条、写回回执与补偿式撤销、对话写回确认与修订记录弹层。
- 工具链：`scripts/verify_all.sh` 增加许可检查；新增 ruff/pre-commit/eslint/coverage 脚本、`THIRD-PARTY-NOTICES.md` 与 Python/Node/vendored 许可门禁，构建时随包复制许可文件。
- 文档纠偏：ARCHITECTURE、DATABASE、AGENT-HANDOFF、README、INSTALL、WINDOWS-PACKAGING 与代码对齐（16 项偏差全部关闭）。
- 回归：811 个 pytest 用例 + 60 个 subtest、149 个 Node 前端测试全部通过；`verify_all.sh` 六步全绿。

## 0.5.5 - 2026-10-15

- 引入分层个人记忆：身份、偏好、规则、经历、领域知识和当前工作分开保存，支持来源、确认状态、软归档和版本留痕。
- 工作区新增个人记忆层入口，助教上下文按问题有界读取稳定规则和相关记忆，Agent 可通过审批动作写回记忆。
- 图谱知识点升级为可穿透知识卡片：补齐证据、方向性关系、修订记录和 1–3 层关系路径查询，右侧详情支持按关系类型筛选。
- 更新架构设计、实施计划和回归测试；保持 SQLite、本地数据和 PC-first 轻量部署边界。

## Unreleased - 2026-10-14

- 增加可选的本机 stdio MCP 只读工具桥接：显式信任、工具白名单、工作区/课程范围、超时和结果限长，加入原有助教工具循环。
- 增加独立 sqlite-vec 索引实验及本地 Embedding CLI；索引与主库分离，查询回查过滤已变更/删除记录，尚未接入正式问答排序。
- 设置页展示扩展实际状态，移除误导性的向量启用占位选项；新增助教可检索的扩展说明。
- 备份排除本机扩展执行配置和可重建索引。此增量尚未更新桌面安装包。

## 0.5.4 - 2026-10-12

- 修复保存模型配置后调用不存在的 renderEngineTag，导致已保存却显示失败的问题。
- 区分配置写入失败与保存后的界面刷新失败；写入失败时不再继续测试旧模型配置。
- 增加模型设置保存及测试流程的前端回归，更新 Mac 独立应用和安装包。

## 0.5.3 - 2026-10-10

- Mac 桌面菜单新增个人资料备份与恢复；恢复前展示摘要、校验归档并保留原资料副本，默认不导出模型凭据。
- 数据迁移拒绝覆盖已有主库，schema 升级增加版本记录、快照及中断恢复。
- 课程上传先归档再进入持久识别队列，支持 OCR 页进度、取消、中断重试；修复全课程扫描 API。
- 工作区选择保存在个人资料中；桌面端口变更后仍能恢复选择。
- 安装构建增加独立 build number、源码摘要、依赖锁与隐私校验；本机签名包不等于 Apple 公证包。

## 0.5.2 - 2026-10-08

- 接入 Personal Agent 上下文编译器：老巴回答会带入当前工作区、课程、目标、结构化日程、有限本地记忆、图谱邻接和待确认动作。
- 将单轮工具预算收紧为最多 8 回合、120 秒，单次运行最多提出 8 个动作，避免本机任务失控。
- 首页 Attention 现在显示当前工作区的待确认知识候选和关系候选，并提供返回课程或图谱的入口。
- 修复来源材料中的章节式标题被误判为目录结构的问题：课堂记录、补充资料确认后现在会进入图谱和掌握度，课程目录/教材标题仍保留为结构节点。
- 重新润色 MinDock 品牌手册的 Personal Agent 定义、课程到图谱闭环和工作流说明，并重新生成 1366×7577 的长图，修复 Logo 加载路径。

## 0.5.0 - 2026-10-05

- Added a lightweight workflow center for composing Agent, Skill, input and approval steps.
- Added workflow CRUD, step editing/reordering, template creation, safe import/export, linear runs, approval, cancel and retry APIs.
- Added the course-material intake and paper-research workflow templates.
- Replaced the static orchestration trace page with a PC-first editor that keeps the step bar horizontal and expands only the selected step.
- Added workflow API and frontend contract tests; workflow exports exclude run data, materials, settings and credentials.
- Updated the MinDock brand manual with a dedicated workflow-center page and regenerated the shareable local source package for this release.

## 0.5.1 - 2026-10-08

- Started the MinDock Personal Agent runtime with profile, goals, bounded runs, action proposals and local trigger scanning.
- Added workspace Today, Attention and Continue surfaces; local writes require an explicit action approval and remain workspace-scoped.
- Reworded the brand manual so the Personal Agent is the continuation layer between workspace context, workflow steps and confirmed writeback.

## 0.4.1 - 2026-10-04

- 提升 PC 图谱可读性：章节聚焦和知识点穿透时保持节点文字为深色，仅弱化无关关系线；普通、关联和激活关系线统一降宽，避免线条压过知识点名称。
- 真实浏览器复核章节聚焦、节点详情和左侧教材导航，Node 23 项与 Python 105 项回归测试通过。
- 明确课程材料流转：拆录候选停留在课程页待确认，支持单材料或当前课程一键确认；确认后节点同时进入正式图谱和掌握度，课程页显示去图谱入口。
- 掌握度页提示未确认候选并返回课程页；日期/通知照片标记为待转为日历事项，不自动混入知识图谱。

## 0.4.0 - 2026-09-30

- Updated the home wordmark to MinDock 思泊 and adopted the new Chinese slogan and AI personal knowledge assistant positioning.
- Added the MinDock brand manual and long-form usage guide with real PC screenshots, blue/orange explanation layers, and a share-ready PNG preview.
- Added the 2026-10-01 quick project review documenting current runtime evidence, packaging boundaries, and remaining gaps.
- Refined the brand manual into a shorter, plain-language flow: Pod-based workspaces, memory-saving conversations, daily-life thought chains, graph navigation, 7–14 day reminders, and Agent Skill customization.
- Reordered the manual so course capture and mastery come before graph growth, enlarged the cover brand lockup, and added the Harbor Agent settings reference.
- Added a dedicated Agent Hub settings page with a live screenshot of the four configured Agents; centered and enlarged the first-page MinDock brand lockup.

- Added first-class StudyPal, LifePal, and WorkPal workspace boundaries with isolated courses, calendar events, conversations, notes, and tasks.
- Added workspace-aware local search, calendar ICS import/export, and Agent tool execution while preserving legacy StudyPal call compatibility.
- Added Obsidian Wiki Schema based Markdown records for workspace notes, including frontmatter, fixed sections, and `[[双向链接]]` references.
- Added per-knowledge-point mastery editing on the mastery page and graph detail panel; every change is written to the revision log.
- Added workspace regression coverage for data isolation, Wiki files, calendar exchange, and search limits.

## 0.3.1 - 2026-09-30

- Reduced StudyPal branding on the home screen so MinDock and its personal knowledge intelligence definition lead the experience.
- Reworked the graph page into a desktop two-column workspace with the graph canvas on the left and node details, editing, and cross-course bridges on the right.
- Kept the graph canvas as the only wheel-zoom surface and made the detail column independently scrollable to prevent accidental graph zoom while reading.

## 0.3.0 - 2026-09-30

- Promoted the reusable knowledge-graph contract from documentation into runtime configuration and storage.
- Added loadable MBA, history, and ecommerce domain profiles under `domains/`, with node types, relation kinds, required fields, Wiki sections, and quality rules.
- Added structured node metadata, aliases, evidence links, relation semantics, contract-aware graph health checks, and graph-page editing for domain attributes and evidence IDs.
- Updated Wiki and dictionary artifacts to v2 metadata while retaining v1 compatibility markers.

## 0.2.3 - 2026-09-30

- Restored the MinDock PC interface to a purple brand system across the top bar, navigation, labels, section title bars, and first-use introduction cover.
- Updated the MinDock wordmark so `M` and `D` are purple while the lowercase letters remain white on the dark purple brand bar.
- Replaced the generic document mark with a Pimage-generated purple `MD` knowledge-connection logo and added PNG favicon assets.

## 0.2.2 - 2026-09-30

- Renamed the visible product brand to MinDock while retaining CyberPal as the technical compatibility name.
- Added the MinDock MD document mark with bright blue M/D brand anchors and an SVG favicon.
- Added the first-use brand introduction dialog and a reusable three-layer definition: dedicated mind system, knowledge accumulation, and long-term personal intelligence companion.
- Updated the bundled platform manual and project documentation to define MinDock as “你的个人知识智能”.

## 0.2.1 - 2026-09-30

- Added reusable course, calendar and notice seed data extracted from the workbench fixtures without depending on the workbench code or database.
- Added a searchable `notices` table and `/api/notices`, preserving the imported notice source in local citations.
- Enhanced the knowledge graph with two-hop focus, animated trace highlighting, focus status, and relation-row traversal buttons.
- Fixed Chinese platform-manual search to match adjacent terms such as “上传课程材料”.

## 0.2.0 - 2026-09-29

- Added local OCR for images and scan-heavy PDFs, plus a Whisper/sidecar transcription adapter for audio.
- Added SQLite FTS5 indexing with deterministic LIKE fallback for knowledge, source chunks, events, and progress.
- Added graph health checks, JSON export/import, and portable graph exchange validation.
- Added Hub Skill creation, safe zip import/export, execution logs, and runtime-only metadata handling.
- Added ICS calendar import/export, source UIDs, and explicit local/incoming conflict resolution.
- Added Windows `start_cyberpal.bat` startup entry and PC-only share documentation.
- Updated the PC operation guide palette: sky-blue explanations, purple screenshot frames, and bright-blue labels.

## 0.1.0 - 2026-09-29

- Consolidated CyberPal project, database, file layout, macOS packaging, and GitHub release standards.
- Added configurable runtime data root through CYBERPAL_DATA_DIR.
- Added bridge link resolution, OCR retry boundary, source status review, and graph relation candidate review.
- Verified textbook ingestion for accounting and explicit OCR-required handling for scanned economics material.
- Archived historical browser output and generated Python caches outside the publishable source tree.
- Verified the live Bing RSS web-search path and clarified its key-free ready state in Settings.
- Added file-backed Hub detail/update APIs with key validation, atomic prompt writes, secret-field rejection, and redacted responses.
- Settings now loads registered skills from `/api/hub` and can enable or disable them without changing the default role routing.
- Added isolated local-install verification coverage; full regression is Python 46/46 and Node 9/9.
- Added review-only model-assisted Wiki drafts with evidence-linked candidates; deterministic notes remain the fallback and formal graph writes still require confirmation.
- Added raw/text source version snapshots, unified diffs, and stale-file restoration with automatic re-indexing.
- Added Hub revision history and rollback APIs for edited Skill/Agent manifests and prompts.
# v0.4.1

- 开始接入 MinDock Personal Agent Runtime：新增个人档案、目标、运行与动作提案 API，以及工作区 Today / Attention / Continue 三个入口。
- 新增本地触发扫描：课程材料候选、未来 14 天日程、等待审批工作流和长期未推进目标按工作区与日期去重；涉及写回的动作必须人工确认。
- 品牌手册重新润色 Personal Agent 定义，明确工作区、课程、工作流、图谱、对话、日历和 Agent Hub 的连续关系。

- 图谱总览继续使用确定性的圆形章节簇布局，节点点击支持关系聚焦与关联点打开。
- 修复 Cytoscape 数值节点 ID 与渲染 ID 不一致造成的原点叠图；章节改为真正的 compound parent node，点击章节进入章节聚焦，点击知识点进入节点穿透。
- 验证普通滚轮不改变图谱缩放，Ctrl/Cmd+滚轮才缩放；左侧 Wiki 目录、中央图谱和右侧详情在 PC 视口内同时可见。
- 新增有教材证据的 Wiki 关系提取，以及带证据页码的 AI 跨章节关系草稿；所有候选必须审核后才进入正式图谱。
- 分享发布改为可直接展开的源码目录结构，不再把源码 ZIP 作为仓库唯一入口。
