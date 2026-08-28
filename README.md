# 智能工作区系统工程（.system）

> **人机协作控制面** · 规则抽象 · 技能生态 · 自动化治理

---

## 📖 核心理念与使用指引（面向人类协作者）

1. **解放双手的下一代人机协作系统**  
   这是一套**彻底解放人操作鼠标动作的人机协作系统**。将繁琐的 UI 点选、表单录入、跨平台状态同步与重复性事务处理，全量转为“自然语言意图输入 + 受控 Agent 自动化生产与治理”的确定性闭环。

2. **零门槛极简上手**  
   无需记忆复杂的命令行参数，只需**使用本地 Agent（如 Claude Code / OMP / Cursor / Codex）直接打开工作区根目录**，即可通过自然语言对话完成待办流转、看板管理、纪要生成与知识沉淀。  
   *（新电脑或非技术同学亦可直接双击 `.system/tools/🚀_一键配置工作区.command` 或对 Agent 说“初始化”一键就绪）*。

---

## 🤖 引导与初始化契约（面向 AI Agent）

当 AI Agent 接入或初始化本工作区时，请严格遵守以下操作协议：

1. **仓库挂载与初始化**  
   将本仓库作为 `.system/` 目录置于工作区根目录下，并首先执行软链接挂载工具：
   ```bash
   python3 .system/tools/bootstrap.py
   ```
   该工具会自动将 `.system/root-configs/` 下的 `AGENTS.md` 与 `CLAUDE.md` 软链接至工作区根目录，建立全局统一的规则控制面。

2. **规则与上下文纪律**  
   - 严格遵循根 `AGENTS.md` 常驻层控制面与 `.system/rules/` 架构规范；
   - 坚持**单一真源（Single Source of Truth）**、**上下文瘦身**与 **YAGNI 原则**，禁止跨层级复制规则或创建冗余文件；
   - 涉及多端任务与项目识别时，以 `.data/registry.md` 实例注册表为唯一映射基准。

---

## 📁 系统目录架构

| 路径 | 职责定位 |
|---|---|
| `root-configs/` | 工作区根入口物理源文件（`AGENTS.md`、`CLAUDE.md`） |
| `rules/` | 跨项目纯抽象规则真源（零外部系统硬编码，纯架构约束） |
| `skills/` | 工作区核心能力扩展库（100% 自包含 Agent Skill） |
| `templates/` | 项目入口、文档结构和机器配置模板 |
| `tools/` | 工作区自愈（`bootstrap.py`）、项目脚手架与健康度检查工具（`lint_workspace.py`） |
| `tests/` | 规则与工具自动化单测 |

> 📌 **数据隔离说明**：工作区实例数据（业务项目注册表、内部战略、凭据）独立存放于 `.data/`（与 `.system/` 同级），严禁提交至公共系统仓库。

---

## 🛠️ 常用开发与治理入口

- **根入口软链自愈**：`python3 .system/tools/bootstrap.py`
- **新项目脚手架初始化**：`python3 .system/tools/init_project.py <项目路径>`
- **工作区规则与健康度体检**：`python3 .system/tools/lint_workspace.py`
- **核心开发与联动规则**：
  - [`.system/rules/开发通用规则.md`](rules/开发通用规则.md)
  - [`.system/rules/开发项目联动规则.md`](rules/开发项目联动规则.md)
  - [`.system/rules/代码库重构与治理规则.md`](rules/代码库重构与治理规则.md)

修改系统规则前请先阅读本目录 `AGENTS.md`；业务项目实现细节应沉淀于项目自身的 README、Spec 或产物中，不反向污染系统抽象层。
