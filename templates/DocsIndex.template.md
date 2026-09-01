# {{项目名}} · 文档导航

> 初始化：{{开始日期}}
> 本页只维护文档入口和真源顺序，不复制专项文档正文。

## 真源顺序

1. 仓库根 `PRODUCT.md`（存在时）：产品边界与长期目标；
2. `02_product/`：需求和产品路线；
3. `03_design/`：方案、交互和原型；
4. `04_architecture/`：架构契约及绑定 task ID 的实施规格；
5. `00_project/`：项目决策、计划和当前执行状态；
6. `01_research/`、`05_reports/`：论证、评审和审计证据。

## 分类

| 目录 | 内容 |
|---|---|
| `00_project/` | 任务、变更记录、项目计划和决策 |
| `01_research/` | 调研、分析、论证和受控研究材料 |
| `02_product/` | 产品目标、需求、路线和介绍材料 |
| `03_design/` | UI/UX、交互、领域方案；原型放 `03_design/mockups/` |
| `04_architecture/` | 架构、接口、数据模型、部署；task spec 放 `04_architecture/specs/` |
| `05_reports/` | 评审、审计、差距评估和测试报告 |
| `06_archive/` | 已被替代且已注明当前真源的历史材料 |

`docs/README.md` 和 `.board.json` 留在 `docs/` 根。高频工作流文件放入 `00_project/`。未经用户明确要求，不因测试或开发验证自动生成独立报告；获准生成的测试报告放入 `05_reports/`。

## 文件命名

新建独立交付物使用 `YYYYMMDD_PascalCaseTopic.ext`，例如：

- `20260819_SystemDesign.md`
- `20260819_ProductMigrationPlan.md`
- `20260819_CodeReview.md`

日期与主旨之间只有一个下划线；主旨使用英文 PascalCase 名词/动词短语，不含空格、连字符或额外下划线。task spec 使用 `TaskID_PascalCaseTopic.md`（不带日期前缀，日期只在档头 `generated: { at: … }` 注明），例如 `Bug-28_StageLogLost.md`。固定入口、工作流文件、机器契约和有正式命名要求的专用模板除外。

> 命名规范唯一真源：交付物命名在工作区 `.system/rules/项目组织.md`，task spec 命名在 `.system/rules/软件工程.md`；本文件是随项目分发的离线副本，规范更新时同步。
