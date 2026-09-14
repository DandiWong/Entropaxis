# Changelog

本文件记录 `project-layout` Skill 的所有显著变更，遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.1.0] - 2026-09-10

### Changed

- 术语统一：「主题工作胶囊」全量更名为「事务胶囊」，与 `.entropaxis/rules/`（项目组织、文件交付、治理指令、角色协作、软件工程）、`front_matter.schema.json` 及体检工具同轮对齐，消除同一机制两个名字的版本漂移。

### Added

- 目录树补充子目录懒创建约束：上图为阶段文件完整形态参照而非初始化清单，`00_原始素材/`、`assets/`、`prototypes/` 等子目录只在材料落盘那一刻创建，脚手架同样不预建空目录。

## [1.0.0] - 2026-09-04

### Added

- 首次发布。从 `.entropaxis/rules/项目组织.md` 下沉结构清单与归位操作：主题工作胶囊目录树、4 域分层结构、`RawInput/` 整理映射表、代码工程 `docs/` 事件容器布局、`README.md` 总纲内容规范。
- `RawInput/` 归位规则由散文列表改写为映射表，便于逐条核对不漏项。

### Changed

- 归属判据（系统与项目边界、项目索引真源、双层流转、决策落盘、Dashboard 边界）保留在规则真源《项目组织》，本 Skill 不重复声明，遵循《01_根系统治理》「领域规则只做编排」准则。
