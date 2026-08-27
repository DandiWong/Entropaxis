---
name: init-project
description: "v1.0.0. 通过简短访谈，在系统工程同级安全初始化时间线驱动的独立项目，建立项目总览、当前状态、知识归档，并关联或创建 Dashboard 项目；开发或文档密集型项目可同时初始化分类 docs 布局。用户要求新建、初始化或 init 项目工作区时使用；拒绝覆盖已有目录。"
metadata:
  version: "1.0.0"
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

项目明显包含代码仓库或需要长期维护大量产品/架构文档时，再确认是否启用分类 `docs/` 布局；普通时间线项目不增加这项追问，也不创建空 `docs/`。

信息不足时允许填写“待补充”，但项目名称必须明确。

初始化前先确认目标目录不存在，并读取
`http://127.0.0.1:8799/api/board`：

- 关联现有项目：确认项目 ID 后，PATCH `/api/projects/{id}` 写入
  `workspace_path`。
- 创建新项目：POST `/api/projects`，至少传 `name` 和预期的
  `workspace_path`；使用响应中的项目 ID。
- 暂不关联：使用“未关联”，不调用 Dashboard 写接口。

Dashboard 不可用时不要直接改 SQLite；完成目录初始化但明确报告尚未关联。

从 `.system` 根目录调用：

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

开发或文档密集型项目在命令末尾增加：

```bash
python3 tools/init_project.py "<项目名>" [其余参数] --docs-layout
```

该选项创建 `docs/README.md` 以及 `00_project/`、`01_research/`、`02_product/`、`03_design/mockups/`、`04_architecture/specs/`、`05_reports/`、`06_archive/`。`docs/README.md` 说明真源顺序与命名规范：独立交付物用 `YYYYMMDD_PascalCaseTopic.ext`；task spec 用 `TaskID_PascalCaseTopic.md`（不带日期前缀，日期在档头 `generated.at`）。本 skill 可能单独分发，此处保留自包含表述；上游真源：交付物命名在 `.system/rules/项目运行规则.md`，task spec 命名在 `.system/rules/开发通用规则.md`，规范更新时同步本段。测试或开发过程不自动生成报告；仅在用户明确要求时创建并按日期命名。

默认在系统工程同级创建项目。只有用户明确指定其他位置时才传 `--workspace`；回溯建档时才传 `--start YYYYMMDD`。

完成后读取项目总览和 Dashboard 项目详情，确认 ID 与
`workspace_path` 一致，再报告绝对路径、创建内容和关联结果。除用户确认的 `--docs-layout` 分类目录外，不创建空事件目录、阶段目录、节点目录或空业务文档。目标已存在时停止，不覆盖、不合并、不自动换名。
