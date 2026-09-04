# CHANGELOG

## [1.3.0] - 2026-09-04

### Added
- 交底书唯一交付格式为 docx：收尾时强制清理 `02_交底书.md` 与中间图片/图表，仅保留 `02_交底书.docx`；Word 导出失败时豁免清理并保留 md 明示。

### Changed
- description 增补中文名称「专利组合拳」并全量中文化；版本号随描述声明（1.3.0）。

## [1.2.0] - 2026-09-04

### Added
- **自包含 Skill 包**：三件套资产内置 `references/`——disclosure（`09知识产权/02_开发/patent-disclosure-skill` @ 2026-09-04，MIT License 随附）与权要指南（PatentWriterAgent `PATENT_SKILL.md` + License）；examples/tests/docs 不随包。
- `scripts/check_env.py`：用时环境自检（核心资产 / playwright / 系统浏览器 / prior-art CLI / verdict 后端 / output_root），`--fix` 仅做两类安全安装（pip playwright、cargo install patent --locked），任何安装失败转入降级矩阵，不阻断 Stage 0-3；支持 `--json` 结构化输出，探针可注入。
- 降级矩阵：CNIPA 路失败 → 人工检索包；dev-tool CLI 缺失 → 检索式清单；verdict 缺失 → `--fast --keyword-only` 搜索级查新；output_root 不可用 → 仅会话交付。

### Changed
- 实例配置收缩：`.data/patent_combo_config.json` 仅必需 `output_root`；`priorart_cli` 与旧版 `disclosure_skill`/`claims_guide` 降为可选覆盖键（legacy 模式）。
- 单测 `tests/test_patent_combo_env.py`：6 例覆盖就绪 / 降级 / 阻断 / 双安装路径 / legacy 覆盖。

## [1.1.0] - 2026-09-03

### Changed
- 入册 Entropaxis（`.system/skills/patent-combo/`），三件套依赖路径全部外置至工作区实例声明 `.data/patent_combo_config.json`，Skill 零路径硬编码，可随根系统分发。
- 注册路由：`~/.pi/agent/skills/patent-combo` 符号链接指向本目录，替换原 02_开发 直注册方式。

## [1.0.0] - 2026-09-03

### Added
- 首次发布 `patent-combo` Skill：四阶段组合拳流水线（脱敏门禁 → rubric 挖点 → 交底书 → 权利要求 + 双路查新），编排复用本地 patent-disclosure-skill v4.1.0、PatentWriterAgent 方法论与 r14dd patent CLI。
