# 系统工程 · Agent 入口

## 上下文

- 工作区元规则与总路由：`../AGENTS.md`
- 本目录说明：`README.md`

## 职责划分

- `rules/`：跨项目通用规则真源（零具体系统绑定，纯架构与行为约束）。
- `templates/`：脚手架模板（项目/应用/契约模板，变量严格闭合）。
- `tools/`：工作区治理、自愈、脚手架与体检脚本。
- `skills/`：自包含 Agent 能力真源（含 `SKILL.md` 与触发元数据）。
- `root-configs/`：根入口唯一物理源（`AGENTS.md`、`CLAUDE.md`）。

## 自迭代标准闭环 (SOP)

1. **归属判别与最小修改**：按元规则确认内容唯一归属；修改 `root-configs/AGENTS.md` 时同步工作区根 `AGENTS.md`。
2. **单一真源与去重**：禁止在 `rules/` 中出现具体业务系统名称或在项目入口复制规则正文。
3. **双重体检门禁（必须通过）**：
   - 单元测试：`python3 -m unittest discover -s tests`（在 `.system` 目录下）
   - 工作区体检：`python3 tools/lint_workspace.py`（14 项指标全部 100% 绿灯）
4. **受控 Git 交付**：
   - 精确 `git add <file>`，严禁 `git add .` 盲目卷入未授权文件；
   - 经用户明确授权后，提交并推送至 `origin/main`。
