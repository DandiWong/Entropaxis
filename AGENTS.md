# 系统工程 · Agent 入口

## 上下文

- 工作区元规则与总路由：`../AGENTS.md`
- 本目录说明：`README.md`
- 修改本目录任何文件：先读 `rules/01_根系统治理.md`，按其「自迭代闭环」执行（含测试、体检与仅限本目录的自动提交）

## 职责划分

- `rules/`：跨项目通用规则真源（零具体系统绑定）
- `schemas/`：机器配置与文档元数据的结构化契约
- `templates/`：`instance/` 渲染到 `data/templates/`，`project/` 为项目脚手架
- `tools/`：治理、自愈、脚手架与体检脚本
- `skills/`：自包含 Agent 能力真源（`SKILL.md` + 触发元数据）
- `entrypoints/`：根入口唯一物理源（`AGENTS.md`、`CLAUDE.md`）
- `data/`：实例数据与凭据（不入库），布局见 `rules/控制面布局.md`

在本目录下运行测试：`python3 -m unittest discover -s tests -t .`；体检：`python3 tools/lint_workspace.py`。
