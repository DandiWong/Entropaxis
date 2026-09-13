# Entropaxis

> Turns entropy into taxis: a human-AI workspace that routes disorder into structured, executable action.

---

**人机协作控制面** · 规则抽象 · 技能生态 · 自动化治理

---

## 📖 核心理念与使用指引（面向人类协作者）

1. **解放双手的下一代人机协作系统**  
   这是一套**彻底解放人操作鼠标动作的人机协作系统**。将繁琐的 UI 点选、表单录入、跨平台状态同步与重复性事务处理，全量转为“自然语言意图输入 + 受控 Agent 自动化生产与治理”的确定性闭环。

2. **零门槛极简上手**  
   无需记忆复杂的命令行参数，只需**使用本地 Agent（如 Claude Code / OMP / Cursor / Codex）直接打开工作区根目录**，即可通过自然语言对话完成待办流转、方案审修（审计/修正）、多模态协作（自定义角色）、看板管理、纪要生成、发票报销与知识沉淀。  
   *（新电脑或非技术同学亦可直接双击一键脚本，或对 Agent 说“初始化”一键就绪：macOS / Linux 用 `.system/tools/🚀_一键配置工作区.command`，Windows 用 `.system/tools/一键配置工作区.bat`）*。
---

## 🤖 引导与初始化契约（面向 AI Agent）

当 AI Agent 接入或初始化本工作区时，请严格遵守以下操作协议：

1. **仓库挂载与初始化**  
   将本仓库作为 `.system/` 目录置于工作区根目录下，并首先执行入口配置同步工具：
   ```bash
   python3 .system/tools/bootstrap.py
   ```
   该工具会自动将 `.system/entrypoints/` 下的 `AGENTS.md` 与 `CLAUDE.md` 物理同步至工作区根目录，建立全局统一的规则控制面（纯文件复制，杜绝云同步网盘跨平台软链冲突）。
2. **规则与上下文纪律**  
   - 严格遵循根 `AGENTS.md` 常驻层控制面与 `.system/rules/` 架构规范；
   - 坚持**单一真源（Single Source of Truth）**、**上下文瘦身**与 **YAGNI 原则**，禁止跨层级复制规则或创建冗余文件；
   - 涉及多端任务与项目识别时，以 `.data/templates/registry.md` 实例注册表为唯一映射基准。

---

## 📁 系统目录架构

| 路径 | 职责定位 |
|---|---|
| `entrypoints/` | 工作区根入口物理源文件（`AGENTS.md`、`CLAUDE.md`） |
| `rules/` | 跨项目纯抽象规则真源（零外部系统硬编码，纯架构约束） |
| `config/` | 由规则正文派生、供工具消费的机器可读控制面数据 |
| `schemas/` | 机器配置与文档元数据的结构化契约 |
| `skills/` | 工作区核心能力扩展库（100% 自包含 Agent Skill） |
| `templates/` | 项目入口、文档结构和机器配置模板 |
| `tools/` | 工作区自愈（`bootstrap.py`）、项目脚手架与健康度检查工具（`lint_workspace.py`） |
| `tests/` | 规则与工具自动化单测 |

> 📌 **数据隔离说明**：工作区实例数据（业务项目注册表、内部战略、凭据）独立存放于 `.data/`（与 `.system/` 同级），严禁提交至公共系统仓库。

> ⚠️ **分发信道（唯一合法路径）**：把本系统交付给他人时，只允许 `git clone`、`git archive` 或 GitHub 版本库 ZIP 下载。**严禁直接拷贝、压缩 `.system/` 目录或经网盘同步**——私有 Skill 靠自带 `.gitignore` 排除出版本库，它们在文件系统上依然物理存在，拷目录会连同内部端点与业务口径一并带走。契约见 [`rules/技能设计.md`](rules/技能设计.md) 6.2 节。

## 🩺 `.system` 健康度指标

健康度是可执行门禁，不做主观打分。`python3 .system/tools/lint_workspace.py` 在既有工作区体检前先检查控制面。

| 指标 | 检查规则 | 失败后的修复动作 |
|---|---|---|
| **结构完整性** | `entrypoints/`、`rules/`、`config/`、`schemas/`、`templates/`、`tools/`、`skills/`、`tests/` 及关键入口均存在 | 从版本库恢复缺失文件，不以项目文件替代系统真源 |
| **根入口同步** | 工作区 `AGENTS.md`、`CLAUDE.md` 内容与 `entrypoints/` 一致 | 运行 `bootstrap.py` 恢复入口 |
| **路由完整性** | `.system` 内显式 Markdown 本地链接、以及行内代码里的 `.system/` / `.data/` 路径引用均可解析（指向 `.data/` 的实例声明按"待落地"计入建议项，不阻断新工作区）| 修正链接或补齐唯一真源 |
| **模板契约** | 通用项目与软件应用脚手架所需模板齐全；变量闭合且属于声明集合 | 修复模板变量，避免生成未渲染文件 |
| **Skill 可发现性** | 每个 Skill 有 `SKILL.md`，`name` 与目录名一致，包含 `description` 触发描述 | 补齐入口或修正元数据 |
| **工具可执行性** | `tools/*.py` 均能通过 Python 编译 | 修复语法错误后再发布系统改动 |
| **分发独立性** | Skill 不把 `.system/` 写成运行前置（控制面 Skill 以 `metadata.scope: control-plane` 自声明豁免）；看板 Provider 声明不含凭证 | 改为相对自身目录定位，或标注为「存在才调用」的可选增强 |
| **声明可兑现** | `.data/` 实例文件声明的来源模板真实存在；声明的写入者（工具/Skill）存在且确实写它 | 同步更正 `stamp_data_provenance.KNOWN`，或让声明的写入者真的落笔 |
| **治理卫生** | 薄常驻、契约水位、规则去重、零系统绑定、注册表登记进度与 `.data/` 声明落地 | 按诊断将事实下沉至唯一真源或运行指定修复工具 |

> 🆕 **全新工作区**：收件方的目录结构与任何既有工作区都不同。项目注册表尚未登记任何项目时，根目录归属只报建议项（体检退出码 0，不红屏）；用 `init_project.py` 立项或在注册表「排除规则」中登记后，该校验自动转为阻断项。共享资料层默认留空，不写死也不探测任何具体目录名。自带 `.git` 的上游/第三方嵌套仓库一律不纳入体检——其内容契约归它自己的 `AGENTS.md`。

前七项属于**阻断项**：任一失败，命令以退出码 `1` 结束；治理卫生属于**建议项**：保留诊断但不阻断使用。指标只检查可客观验证的控制面契约；业务内容质量与项目进度不以此工具裁决。

---

## 🪟 Windows 安装常见问题（人类须知！！！）

### 下载代码包

这是一个私有仓库，只有受邀成员才能访问。如果 AI 助手提示"下载失败"或"无法访问"，**不用理会报错信息**，按以下步骤手动下载即可：

1. 用浏览器打开 `https://github.com/DandiWong/Entropaxis`（确保已登录 GitHub）
2. 点击绿色的 **Code** 按钮 → **Download ZIP**
3. 把下载好的 ZIP 文件交给 AI 助手，让它帮你解压到工作区

### 一键初始化

解压到位后，进入 `.system\tools\`，双击 **`一键配置工作区.bat`**（不是 `.command`，那个是 Mac 用的）。脚本会自动找 `py -3` / `python3` / `python`；提示没装 Python 时按提示装完再双击一次即可。

### 让 AI 助手帮你操作网页

如果你希望 AI 助手直接帮你在浏览器里操作（比如帮你点按钮、填表单），需要先按以下步骤开启一个特殊模式，让 AI 助手能"看到"你的浏览器：

1. 完全关闭 Chrome（任务栏右键 → 退出）
2. 重新打开 Chrome，然后告诉 AI 助手"Chrome 已重启，请继续"

> 如果这一步太麻烦，直接告诉 AI 助手"我自己操作浏览器，你告诉我点哪里"就好，不必强行让 AI 接管。

### 其他已知问题（Agent 决策参考）

| 症状 | 处理方式 |
|------|---------|
| PowerShell 返回空输出或报错 | 改用 `cmd /c <命令>` 执行，或用 `Out-File` 将输出写入临时文件再读取 |
| `.system` / `.data` 在文件管理器中可见 | 执行 `attrib +h +s <路径>\.system` 和 `attrib +h +s <路径>\.data` |

> `bootstrap.py` 在 Windows 下运行完成后会自动打印上述提示。

---

## 🛠️ 常用开发与治理入口

- **根入口配置同步/自愈**：`python3 .system/tools/bootstrap.py`
- **角色模态与外部 Agent 交互配置**：`python3 .system/tools/setup_agents.py`（支持 `--scan` 探测 CLI、`--verify` 校验角色、`--apply-preset` 应用预设）
- **指令路由与静态上下文审计**：`python3 .system/tools/audit_routing.py --strict`（回归集门禁与冻结留出诊断分列；`--json` 输出完整 Hook、去重后的必需读取区间、未知项。静态估算不代表模型 usage 或费用）
- **工作区规则与健康度体检**：`python3 .system/tools/lint_workspace.py`
- **分发就绪核验（收件方视角）**：`python3 .system/tools/check_distribution.py`
- **方案审计门禁校验**：`python3 .system/tools/check_audit_gate.py <审计报告路径>`
- **按配置打开文件**：`python3 .system/tools/open_file.py <path> [<path> ...]`
- **通用项目脚手架初始化**：`python3 .system/tools/init_project.py <项目名>`
- **软件应用脚手架初始化**：`python3 .system/tools/init_app.py <app-name> --target-dir <项目路径>/02_开发`
- **路由读取契约**：`config/route_map.json` 使用 `reads: [{file, anchor}]`，`anchor: null` 表示全文；每个必需依赖显式列出，条件依赖按动作追加。Hook 和审计共用 `tools/route_context.py`，保留适用范围、合并重叠区间；锚点异常回退会留痕并由体检检出。
- **语料维护**：`tests/fixtures/instruction_cases.json` 是已登记回归集；`instruction_holdout.json` 是冻结的构造样本诊断，不据它调词或刷分。用留出集参与调参后必须保留其身份并另设新组，不能继续宣称未见数据。
- **核心开发与治理规则**：
  - [`.system/rules/00_元规则.md`](rules/00_元规则.md)
  - [`.system/rules/01_根系统治理.md`](rules/01_根系统治理.md)
  - [`.system/rules/角色协作.md`](rules/角色协作.md)
  - [`.system/rules/治理指令.md`](rules/治理指令.md)
  - [`.system/rules/工作流指令.md`](rules/工作流指令.md)
  - [`.system/rules/软件工程.md`](rules/软件工程.md)
  - [`.system/rules/看板联动.md`](rules/看板联动.md)
修改系统规则前请先阅读本目录 `AGENTS.md`；业务项目实现细节应沉淀于项目自身的 README、Spec 或产物中，不反向污染系统抽象层。
