# CHANGELOG

## 1.1.0

- 对根系统零依赖（《技能设计》6.2 独立运行铁律）：打开闭环改为「工作区提供 `.system/tools/open_file.py` 则调用，否则回报绝对路径」，不再硬依赖 `.system/`；自身脚本路径改为相对 `<skill-dir>` 定位，脱离工作区独立安装亦可运行。

## [1.0.0] - 2026-08-31

### Added
- 首次发布 `ppt-design` Skill（极简高管汇报级 PPT 设计规范与生成工具）。
- 确立“无框无界、字阶定秩序、高管决策导向、流程单向箭头”四大核心设计铁律。
- 固化标准 16:9 画布色彩系统（`C_DEEP_BLUE`、`C_ACCENT_BLUE`、`C_ORANGE`、`C_ARROW` 等 Token）。
- 提供四大标准页面排版配方：
  1. `3-Metric` 现状基线与业务痛点页；
  2. `3-Pillar` 业务定位与解法页；
  3. `6-Stage` 业务流水线与流转箭头页；
  4. `4-Value` 价值对照与落地决策页（含 Ask 模块）。
- 提供基于 `python-pptx` 的开箱即用原生生成脚本 `templates/generate_deck.py`。
