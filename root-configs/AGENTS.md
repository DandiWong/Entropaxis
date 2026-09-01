# 工作区 · Agent 入口

本文件是工作区规则控制面。项目入口只补充本项目差异；跨项目规则只在 `.system/rules/` 维护。

元规则：[`.system/rules/00_元规则.md`](.system/rules/00_元规则.md)

## 工具

- 搜索用 `grep`/`find` 内置工具，多OR词用一次 `multi_grep`；必须走bash 时用 `rg`，不用 `grep`
- 定位后用 `read` 的offset/limit 只读命中附近；工作区外已知文件直接read

## 安全边界

- 修改 `.system/` 下任何文件前，必须先读 `.system/rules/01_根系统治理.md`，按 SOP 执行。
- 发送、发布、推送、部署、删除、覆盖或向公共系统写入前，必须取得明确授权。
- 外发内容、个人信息和医疗相关材料默认先生成草稿。
- 修改前检查版本库与工作区状态，只触碰本次任务范围。
- 信息不足时先说明已知、未知与假设；不得把推断写成事实。

## 按需路由

| 任务 / 触发行为 | 真源 |
|---|---|
| 新建/修改/生成/导出通用文件（PPTX/DOCX/XLSX/PDF/图片/独立文档） | `.system/rules/文件交付.md` |
| 系统边界、项目目录结构与组织命名 | `.system/rules/项目组织.md` |
| 代码开发、调试与自动化验证 | `.system/rules/软件工程.md` |
| Spec、任务状态与外部看板联动 | `.system/rules/看板联动.md` |
| 根系统、AGENTS 与元规则治理 | `.system/rules/01_根系统治理.md` |
| 角色职责、跨角色交接与协作验收 | `.system/rules/角色协作.md` |
| 工作区自然语言指令与任务解析 | `.system/rules/指令解析.md` |
| 表达文风与可视化 | `.system/rules/表达文风.md` |
| 对外发布与审批 | `.system/rules/对外发布.md` |
| 知识沉淀与 Wiki 录入 | `.system/rules/知识沉淀.md` |
| Skill 规范与维护 | `.system/rules/技能维护.md` |
| 任务看板 / 待办管理 | `.system/rules/看板联动.md`（按需调用看板 Skill 或本地适配器） |
| 会议纪要生成 | `.system/rules/指令解析.md`（按需调用会议纪要 Skill） |
| 财务报销与发票归总 | `.system/rules/财务报销.md` |
| 项目注册表（工作区项目索引） | `.data/registry.md` |

进入具体项目后，先读最近的 `AGENTS.md`，再按其中路由读取 README、Spec 或操作文档。仅在任务涉及外部看板、联网、发布或特定工具时加载对应规则或 Skill。
