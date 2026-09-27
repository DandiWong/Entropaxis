# Changelog - init-project Skill

## [1.2.1] - 2026-09-27
### Changed
- 立项登记目标由 `registry.md` 表格行改为 `registry.yaml` 的 `projects` 列表项。

## [1.2.0] - 2026-09-26
### Changed
- 补记：此前版本号未随改动登记（description 已写 v1.2.0、metadata 停在 1.1.0），以下据 git 历史归纳。
- 需要代码工程时改在 `03_工程研发/<app>/` 以独立软件应用脚手架初始化（PRODUCT、tasks、src、test 与 docs）。
- Skill 配置自包含，不再读写控制面 `data/`。

## [1.1.0] - 2026-09-03
### Changed
- 与具体看板 CLI 解耦，项目联动改为按已安装看板 Skill 可选接入。

## [1.0.0] - 2026-08-25
### Added
- 初始版本：支持通过访谈安全初始化时间线驱动的独立项目，建立项目总览、当前状态、知识归档，并联动 Dashboard 项目。
