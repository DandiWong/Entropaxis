---
name: init-project
description: "v1.1.0. 通过简短访谈安全初始化时间线驱动的通用业务/综合项目（5 域 + 2 契约 + RawInput）；需要代码工程时，改在 02_开发/<app>/ 以独立软件应用脚手架初始化 PRODUCT、tasks、src、test 与 docs。用户要求新建、初始化或 init 项目/应用工作区时使用；拒绝覆盖已有目录。"
metadata:
  version: "1.1.0"
---

# 初始化项目

先收集信息，用户已经提供的不要重复询问：

1. 项目名称是什么？
2. 项目用途和具体目标是什么？
3. 项目负责人及相关人员是谁？
4. 下一关键事件是什么？
5. 已有材料在哪里？
6. 有哪些项目独有知识，以及需要引用哪些上层共享知识路径？
7. 是否涉及内部敏感、个人或医疗数据、拟对外发布？
8. Dashboard 要关联现有项目、创建新项目，还是暂不关联？

通用项目始终初始化标准 5 域结构；软件工程代码库只在用户明确需要时，以独立应用脚手架创建在 `02_开发/<app>/`，不在项目根创建 `docs/`。

信息不足时允许填写“待补充”，但项目名称必须明确。

初始化前先确认目标目录不存在，并读取
`http://127.0.0.1:8799/api/board`：

- 关联现有项目：确认项目 ID 后，PATCH `/api/projects/{id}` 写入
  `workspace_path`。
- 创建新项目：POST `/api/projects`，至少传 `name` 和预期的
  `workspace_path`；使用响应中的项目 ID。
- 暂不关联：使用“未关联”，不调用 Dashboard 写接口。

Dashboard 不可用时不要直接改 SQLite；完成目录初始化但明确报告尚未关联。

从 `.system` 根目录调用（初始化通用业务/综合项目）：

```bash
python3 tools/init_project.py "<项目名>" \
  --purpose-goal "<用途和目标>" \
  --people "<负责人及相关人员>" \
  --next-event "<下一关键事件>" \
  --materials "<已有材料位置>" \
  --project-knowledge "<项目独有知识>" \
  --shared-knowledge "<共享知识路径>" \
  --sensitivity "<敏感级别>" \
  --dashboard-project-id "<Dashboard返回或确认的项目ID>"
```

该命令创建标准的通用项目分层结构（`_契约/`、`RawInput/`、`00_材料/`、`01_项目/`、`02_开发/`、`03_交付/`、`Archive/` 以及 `_项目总览.md`、`_契约/当前状态.md`、`AGENTS.md`、`CLAUDE.md`）。

当项目需要在 `02_开发/<app>/` 下初始化独立软件工程代码仓库时，调用软件工程脚手架：

```bash
python3 tools/init_app.py "<app-name>" --target-dir "<项目路径>/02_开发" --purpose "<定位与价值>"
```

该命令独立创建代码工程骨架（`PRODUCT.md`、`tasks.md`、`src/`、`test/` 以及 `docs/00_project ~ 06_archive` 分类规范）。

完成后读取项目总览和 Dashboard 项目详情，确认 ID 与 `workspace_path` 一致，再报告绝对路径、创建内容和关联结果。目标已存在时停止，不覆盖、不合并、不自动换名。
