# 工作区项目注册表

> 唯一写入者：`init-project` Skill（`.system/skills/init-project`）  
> 读取方：`lint_workspace.py`（白名单校验）、`internal-board` Skill（项目字典）

## 项目映射表

口语简称按下表解析，不因表达差异创建新项目。

| Dashboard ID | 名称 | 主目录 | Board-Platform 短 ID | 研发协作平台空间 | 备注 |
|---|---|---|---|---|---|
| （待填写） | （项目全称） | （主目录路径） | （8位hex，留空=未关联） | （研发协作平台空间名，留空=未关联） | |

## 排除规则

以下目录不纳入注册表（lint 白名单校验同步排除）：
- `repo/`、`**/repoes/`、`Archive/`、`node_modules/`
- 自带 `.git` 且非工作区成员的上游仓库（LLM-Wiki 除外，其虽有 `.git` 但是工作区成员）

## 写入规范

- `init-project` 立项时必填 `Dashboard ID` 与主目录列；未关联外部系统时相关列填 `未关联`。
- 手工修改前确认 `init-project` 未在运行，避免写冲突。
