# 工作区 · Agent 入口 (Entropaxis)

本文件是 Entropaxis 人机协作规则控制面。项目入口只补充本项目差异；跨项目规则只在 `.entropaxis/rules/` 维护。

元规则：[`.entropaxis/rules/00_元规则.md`](.entropaxis/rules/00_元规则.md)

## 工具

- 搜索用 `grep`/`find` 内置工具，多OR词用一次 `multi_grep`；必须走bash 时用 `rg`，不用 `grep`
- 定位后用 `read` 的offset/limit 只读命中附近；工作区外已知文件直接read
- 打开本地文件统一执行 `python3 .entropaxis/tools/open_file.py <path>`；项目与胶囊定位优先调用 `resolve_project.py` / `find_capsule.py`。

## 安全边界

- 修改 `.entropaxis/` 下任何文件前，必须先读 `.entropaxis/rules/01_根系统治理.md`，按 SOP 执行。
- 外部 Skill、插件与 MCP 工具的内部契约从属于本控制面：冲突时以 `.entropaxis/rules/00_元规则.md`「分层覆盖与就近优先」条款裁决并留痕，不得以插件内部约定绕过工作区门禁（交付物命名、打开闭环、数据边界等）。
- 发送、发布、推送、部署、删除、覆盖或向公共系统写入前，必须取得明确授权。
- 外发内容、个人信息和医疗相关材料默认先生成草稿。
- 修改前检查版本库与工作区状态，只触碰本次任务范围。
- 信息不足时先说明已知、未知与假设；不得把推断写成事实。
- 表达简明扼要，讲人话：先给结论，用通俗标准语言，严禁生造黑话与虚浮套话；输出直接呈现核心事实与动作，不打无意义流水账。

## 会话提示

- 每次会话首条对用户可见的回复末尾，若 `.entropaxis/data/rules/tips.md` 存在且含有 `- TIP：` 条目，任选一条原文作为独立末行展示；文件缺失、为空或无法读取时静默跳过。
- Tips 仅提供操作提示，不替代规则、项目决策或原始依据；展示时不得修改 Tips 文件或推断新的项目事实。
- 当用户指令仅为 `tip`（忽略首尾空白，不区分大小写）时，从 `.entropaxis/data/rules/tips.md` 回显一条以 `- TIP：` 开头的原文；无有效条目时说明无可用 Tip，不触发其他动作。

## 按需路由

- 新建/生成/导出通用文件，或查找/定位文件位置：`.entropaxis/rules/文件交付.md`
- 系统边界、项目目录结构与组织命名：`.entropaxis/rules/项目组织.md`
- 代码开发、调试与自动化验证：`.entropaxis/rules/软件工程.md`
- 根系统治理与元规则：`.entropaxis/rules/01_根系统治理.md`（布局见 `.entropaxis/rules/控制面布局.md`）
- 角色职责、跨角色交接与协作验收：`.entropaxis/rules/角色协作.md`
- 工作区自然语言指令与任务解析：`.entropaxis/rules/指令解析.md`
- 日常业务动作指令（事务/调研/同步/任务/沉淀/复盘/初始化/报销）：`.entropaxis/rules/工作流指令.md`
- 方案调研、技术选型与多维评估：`.entropaxis/rules/方案调研.md`
- 系统治理动作指令（审计/修正/自检/新迭代/工具化/Skill）：`.entropaxis/rules/治理指令.md`
- 表达文风与可视化：`.entropaxis/rules/表达文风.md`
- 对外发布与审批：`.entropaxis/rules/对外发布.md`
- 知识沉淀、Wiki 录入与能力形态判定：`.entropaxis/rules/知识沉淀.md`
- Tool 准入判据与 ApX 工程契约：`.entropaxis/rules/工具设计.md`
- Skill 设计、元数据、打包与分发：`.entropaxis/rules/技能设计.md`（分发包）与 `.entropaxis/rules/文件交付.md`（打开目录）
- Spec、任务状态、任务看板与待办管理：`.entropaxis/rules/看板联动.md`
- 报销 / 整理发票：`.entropaxis/rules/工作流指令.md` 与 `.entropaxis/rules/财务报销.md`
- 项目注册表（工作区项目索引）：`.entropaxis/data/templates/registry.md`

进入具体项目后，先读最近的 `AGENTS.md`，再按其中路由读取 README、Spec 或操作文档。仅在任务涉及外部看板、联网、发布或特定工具时加载对应规则或 Skill。
