---
name: init-project
description: "v1.2.0. 通过简短访谈安全初始化时间线驱动的通用业务/综合项目（4 域 + 2 契约 + RawInput + Archive）；需要代码工程时，改在 03_工程研发/<app>/ 以独立软件应用脚手架初始化 PRODUCT、tasks、src、test 与 docs。用户要求新建、初始化或 init 项目/应用工作区时使用；拒绝覆盖已有目录。"
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

通用项目始终初始化标准 4 域结构（`01_项目管理`、`02_产品设计`、`03_工程研发`、`04_运营增长`）；软件工程代码库只在用户明确需要时，以独立应用脚手架创建在 `03_工程研发/<app>/`，不在项目根创建 `docs/`。

信息不足时允许填写“待补充”，但项目名称必须明确。

初始化前先确认目标目录不存在，并按 `internal-board` Skill 的约定经 `dash.py` 关联或创建 Dashboard 项目（不手写 HTTP 请求，不走本地端口）：

- 关联现有项目：`dash.py project get <id>` 确认项目存在，把项目 ID 写入本项目 `docs/.board.json`（结构见《看板联动.md》）。
- 创建新项目：`dash.py project add --name "<项目名>"`，取响应中的项目 ID 写入 `docs/.board.json`。
- 暂不关联：使用“未关联”，不调用 Dashboard 写接口。

Dashboard 不可用或未安装 `internal-board` Skill 时不要直接改 SQLite；完成目录初始化但明确报告尚未关联，优雅降级为纯本地离线初始化。

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

该命令创建标准的通用项目分层结构（`RawInput/`、`01_项目管理/`、`02_产品设计/`、`03_工程研发/`、`04_运营增长/`、`Archive/` 以及 `README.md`、`01_项目管理/DECISIONS.md`、`AGENTS.md`、`CLAUDE.md`）。

当项目需要在 `03_工程研发/<app>/` 下初始化独立软件工程代码仓库时，调用软件工程脚手架：

```bash
python3 tools/init_app.py "<app-name>" --target-dir "<项目路径>/03_工程研发" --purpose "<定位与价值>"
```

该命令独立创建代码工程骨架（`PRODUCT.md`、`src/`、`test/`、`docs/Tasks.md`、`docs/Changelog.md`、`docs/Benchmark.md`、`docs/ReleaseNote.md`；工程事项按 `docs/YYYYMMDD_主题/` 容器落盘）。

完成后核对 `docs/.board.json` 中记录的项目 ID 与 `dash.py project get <id>` 返回一致，再报告绝对路径、创建内容和关联结果。目标已存在时停止，不覆盖、不合并、不自动换名。
