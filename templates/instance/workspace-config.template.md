# 工作区配置（环境与组织参数）

> `.entropaxis/rules/` 的参数真源之一：规则正文只含通用逻辑，工作区实例参数统一放本文件；移植到其他环境时仅需替换本文件（与 `reimbursement-config.md` 同构）。

## 共享资料层

不属于任何 Dashboard 项目、跨项目共享的资料目录（知识库规则、项目运行规则的共享层判定以此为准）：

- `{{ORG_SHARED_DIR_1}}`：{{ORG_SHARED_PURPOSE_1}}
- `{{ORG_SHARED_DIR_2}}`：{{ORG_SHARED_PURPOSE_2}}
- `{{ORG_SHARED_DIR_3}}`：{{ORG_SHARED_PURPOSE_3}}

> **默认为空**：多数工作区没有跨项目共享目录，各使用者的目录结构各不相同。确有共享资料层时把占位行改为实际目录名；没有就整行删掉，不影响任何规则生效。

## 组织名称默认口径

- 默认名称：{{ORG_FULL_NAME}}
- 禁用缩写：{{ORG_FORBIDDEN_ABBR}}
- 例外：对外正式材料以收件方或官方品牌要求为准

> 首次渲染后请把上述两个必填字段改为本工作区所属组织/公司的真实名称与禁用缩写；不改时新建文档默认使用工作区目录名兜底。

## 角色模态外置 CLI 与模型声明

当认知模态外置为独立 CLI/Agent 进程承担时（跨 CLI 协作，见 `.entropaxis/rules/角色协作.md`），本机生效的启动命令。未配置或指定为 `subagent` 时默认使用当前 Agent 的内置 Subagent 机制：

- Reviewer（方案审计/对抗评审/架构合规）：subagent · 启动命令: 内置 Subagent 机制 (auto)
- Researcher（调研/文献综述）：subagent · 启动命令: 内置 Subagent 机制 (auto)
- Builder（方案实施/核心编码/重构）：subagent · 启动命令: 内置 Subagent 机制 (auto)
- Designer（方案设计/原型 demo）：subagent · 启动命令: 内置 Subagent 机制 (auto)
- Maintainer（汇报落盘/守门验收/证据核验）：subagent · 启动命令: 内置 Subagent 机制 (auto)

> 首次初始化后默认全部使用内置 Subagent。可通过 `python3 .entropaxis/tools/setup_agents.py` 交互式检测宿主机已安装的外部 Agent CLI 并自动配置。
## 角色调度参数（三级解析第 3 级）

> 承载调度的参数真源（`rules/角色协作.md`「角色指派三级解析与调度门禁」；机器契约 `schemas/command_profile.schema.json`）。`default_dispatch_mode` 仅用户可改；`command_profiles` 由 `setup_agents.py --migrate-command-profiles` 从上表迁移生成（保守判定，拒迁项标 needs-manual-conversion），亦可手工维护。

```yaml
default_dispatch_mode: strict

command_profiles: {}

dispatch_authorizations: []
```
