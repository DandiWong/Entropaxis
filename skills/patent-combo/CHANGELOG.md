# CHANGELOG

## [1.1.0] - 2026-09-03

### Changed
- 入册 Entropaxis（`.system/skills/patent-combo/`），三件套依赖路径全部外置至工作区实例声明 `.data/patent_combo_config.json`，Skill 零路径硬编码，可随根系统分发。
- 注册路由：`~/.pi/agent/skills/patent-combo` 符号链接指向本目录，替换原 02_开发 直注册方式。

## [1.0.0] - 2026-09-03

### Added
- 首次发布 `patent-combo` Skill：四阶段组合拳流水线（脱敏门禁 → rubric 挖点 → 交底书 → 权利要求 + 双路查新），编排复用本地 patent-disclosure-skill v4.1.0、PatentWriterAgent 方法论与 r14dd patent CLI。
