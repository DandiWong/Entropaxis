# 工作区项目注册表

> 映射表写入者：`init_project.py` 立项时自动追加一行（append-only，已登记同名项目即跳过）；其余内容与「排除规则」由人工维护  
> 读取方：`lint_workspace.py`（白名单校验）、外部看板联动工具（项目字典）

## 项目映射表

口语简称按下表解析，不因表达差异创建新项目。

| 项目 ID | 名称 | 主目录 | 外部看板映射 (Key-Value) | 备注 |
|---|---|---|---|---|
| （待填写） | （项目全称） | （主目录路径） | main=<ID>; dev=<ID>; ext=<ID>（留空=未关联） | |

## 外部看板映射规范 (Key-Value)

支持以分号或空格分隔的多系统键值对映射（如 `main=proj_1; dev=iss_8f2; ext=space_qa`），由本地 `.entropaxis/data/templates/board_config.json` 或项目 `docs/.board.json` 解释具体 Provider：
- `main`：团队主看板 / 交付看板 ID
- `dev`：研发过程 / 轻量 Issue 看板 ID
- `ext`：企业协作空间 / 第三方平台空间名
- 更多自定义系统可自由追加键值对（例如 `linear=TEAM-1; feishu=space_abc; jira=PROJ-10`）

## 排除规则

以下目录不纳入注册表（lint 白名单校验同步排除）：
- `repo/`、`**/repoes/`、`Archive/`、`node_modules/`
- 自带 `.git` 且非工作区成员的上游仓库（LLM-Wiki 除外，其虽有 `.git` 但是工作区成员）

## 写入规范

- `init-project` 立项时必填 `项目 ID` 与主目录列；未关联外部系统时映射列填 `未关联` 或留空。
- 手工修改前确认 `init-project` 未在运行，避免写冲突。
