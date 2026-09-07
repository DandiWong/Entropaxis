# {{项目名}} · 文档导航

> 初始化：{{开始日期}}
> 本页只维护文档入口和真源顺序，不复制专项文档正文。

## 真源顺序

1. 仓库根 `PRODUCT.md`（存在时）：产品边界与长期目标；
2. 项目根 `01_项目管理/`、`02_产品设计/`、`04_运营增长/`：项目计划、调研、产品、视觉与运营材料；
3. `YYYYMMDD_主题/`：与代码实现绑定的规格、审计和评估证据。

## 工程工作流文件

| 文件 | 内容 |
|---|---|
| `Tasks.md` | 应用实现任务与规划真源 |
| `Changelog.md` | 研发变更履历 |
| `Benchmark.md` | 可复现量化成效评估 |
| `ReleaseNote.md` | 工程版本发版说明 |

## 工程事项容器

```text
docs/YYYYMMDD_主题/
├── 04_Spec_<ID>.md
├── Audit_<ID>_主题方案审计.md
└── Report_<ID>_主题评估报告.md
```

独立 Spec（不在日期主题目录内）采用 `<ID>_中文主题.md`；两种命名下 `<ID>` 均与配套的 `Audit_<ID>_*.md` 一一对应。既有 `Spec_<ID>_主题方案.md` 命名保留只读识别，不批量改名。

三个文件按实际产出创建，不预建空文件；同一事项使用相同的 Task ID 与主题。`docs/README.md` 和 `.board.json` 留在 `docs/` 根。产品、视觉、调研、项目计划、普通报告和归档材料归项目根对应领域，不在 `docs/` 复制。未经用户明确要求，不因测试或开发验证自动生成独立报告。

## 文件命名

新建独立交付物使用 `YYYYMMDD_PascalCaseTopic.ext`，例如：

- `20260819_SystemDesign.md`
- `20260819_ProductMigrationPlan.md`
- `20260819_CodeReview.md`

日期与主旨之间只有一个下划线；主旨使用英文 PascalCase 名词/动词短语，不含空格、连字符或额外下划线。task spec 使用 `TaskID_PascalCaseTopic.md`（不带日期前缀，日期只在档头 `generated: { at: … }` 注明），例如 `Bug-28_StageLogLost.md`。固定入口、工作流文件、机器契约和有正式命名要求的专用模板除外。

> 命名规范唯一真源：交付物命名在工作区 `.system/rules/项目组织.md`，task spec 命名在 `.system/rules/软件工程.md`；本文件是随项目分发的离线副本，规范更新时同步。
