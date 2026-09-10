# 工作区配置（环境与组织参数）

> `.system/rules/` 的参数真源之一：规则正文只含通用逻辑，工作区实例参数统一放本文件；移植到其他环境时仅需替换本文件（与 `reimbursement-config.md` 同构）。

## 共享资料层

不属于任何 Dashboard 项目、跨项目共享的资料目录（知识库规则、项目运行规则的共享层判定以此为准）：

| 目录 | 用途 |
|---|---|
| `{{ORG_SHARED_DIR_1}}` | {{ORG_SHARED_PURPOSE_1}} |
| `{{ORG_SHARED_DIR_2}}` | {{ORG_SHARED_PURPOSE_2}} |
| `{{ORG_SHARED_DIR_3}}` | {{ORG_SHARED_PURPOSE_3}} |

> 模板首次渲染时目录占位会被替换为工作区目录名（兜底），用户可按需改为实际共享资料目录名。

## 组织名称默认口径

| 参数 | 值 |
|---|---|
| 默认名称 | {{ORG_FULL_NAME}} |
| 禁用缩写 | {{ORG_FORBIDDEN_ABBR}} |
| 例外 | 对外正式材料以收件方或官方品牌要求为准 |

> 首次渲染后请把上述两个必填字段改为本工作区所属组织/公司的真实名称与禁用缩写；不改时新建文档默认使用工作区目录名兜底。

## 角色模态外置 CLI 与模型声明

当认知模态外置为独立 CLI/Agent 进程承担时（跨 CLI 协作，见 `.system/rules/角色协作.md`），本机生效的启动命令。未配置或指定为 `subagent` 时默认使用当前 Agent 的内置 Subagent 机制：

| 角色模态 | 职责定位 | 承载 CLI | 启动命令 |
|---|---|---|---|
| Reviewer | 方案审计/对抗评审/架构合规 | subagent | 内置 Subagent 机制 (auto) |
| Researcher | 调研（可联网）/文献综述 | subagent | 内置 Subagent 机制 (auto) |
| Builder | 方案实施/核心编码/重构 | subagent | 内置 Subagent 机制 (auto) |
| Designer | 方案设计/原型 demo | subagent | 内置 Subagent 机制 (auto) |
| Maintainer | 汇报落盘/守门验收/证据核验 | subagent | 内置 Subagent 机制 (auto) |

> 首次初始化后默认全部使用内置 Subagent。可通过 `python3 .system/tools/setup_agents.py` 交互式检测宿主机已安装的外部 Agent CLI 并自动配置。