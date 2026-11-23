# CyberPal 领域档案

领域档案是知识图谱的可替换配置，不是另一套代码。平台核心合同固定来源、证据、节点、关系、候选、审核、修订和版本；这里定义不同场景的节点类型、关系范围、必填字段、质量规则和 Wiki 段落。

- `mba.json`：StudyPal 当前 MBA 学习场景。
- `history.json`：HistoryPal 历史资料场景。
- `ecommerce.json`：电商经营场景示例。
- `work.json`：WorkPal 工作 Wiki 与组织方法场景。
- `life.json`：LifePal 个人记录与生活经验场景，无 MBA 考点要求。
- `general.json`：未知显式工作区的中性回退，无默认工作区归属。

## 路由与兼容

`profile_for_course(course, workspace)` 的工作区参数去除首尾空白、忽略大小写后精确匹配。明确工作区时，标题仅能在该工作区配置的领域内匹配，不能跳到其他工作区的档案；例如 WorkPal 下的“组织管理”仍使用 `work`，LifePal 下的“MBA 计划”使用 `life`。未知显式工作区使用 `general`，不会根据标题猜成 MBA。

旧 Wiki 调用会在第二参数传入已选领域 ID（如 `work`、`history`），继续支持精确 ID 选择。工作区名称匹配优先于领域 ID。没有传工作区（包括空白串）时，保持原来的课程标题推断和 MBA 缺省回退；旧课程调用未提供真实工作区时，路由无法判断其是否属于个人 Work/Life。

领域不是工作区身份。`workspace` 是档案的路由提示，不是数据所有权或授权依据，不能据此重写已有数据的工作区。StudyPal、WorkPal、LifePal 是工作区；企业 AI 可以作为 WorkPal 内的另一个领域，不能因此新建第四个空间。`general` 的 `workspace` 为空串，表示不声称任何工作区归属。历史与电商示例中的工作区提示也不会创建真实工作区。

同一工作区可挂多个领域：只有一个档案时直接作为缺省；多个档案时必须且只能有一个 `workspace_default: true`（WorkPal 当前为 `work`）。在工作区内先匹配 `course_patterns`，未命中才使用缺省。多个标题同时命中时保留按配置文件名排序的旧优先顺序。领域 ID 仍可直接选取子领域。

## 配置校验

新增领域时复制一份 JSON，先通过 `load_profiles()` 或 `GET /api/knowledge/profile` 校验。不要在代码里新增领域分支。配置按文件名排序读取并缓存；修改后调用 `clear_cache()` 或重启进程。

- 根必须是对象，必填键保持现有合同；重复 JSON 键（包括嵌套键）和重复领域 ID 均拒绝，错误包含文件名。
- `id` 使用现有小写标识规则；`version` 为语义版本号（如 `1.0.0`、`1.1.0-rc.1`）；`workspace` 和 `display_name` 必须是字符串，只有 `workspace` 可为空串。
- 节点/关系必须使用平台注册类型；数组项必须是非空字符串且无重复，`course_patterns` 重复判断忽略大小写。`required_fields` 仍必须包含 `source_refs`，质量规则不可为空。
- 可选 `course_patterns`、`extraction_sections` 允许省略或为空数组；`workspace_default` 如提供必须为布尔值，且标记默认时必须有工作区提示。
- `wiki_rules` 必须包含非空字符串 `frontmatter_schema`、`link_style`、`evidence_locator`；其他扩展字段继续保留，不新增全局注册表或数据库迁移。
