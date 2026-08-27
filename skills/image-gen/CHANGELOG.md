# Changelog

All notable changes to the `image-gen` skill will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-08-26

### Added
- 初始版本发布。
- 提供 `scripts/gen.py` 纯标准库脚本，跨环境自适应发现 Codex 生成图片目录（含 `$CODEX_HOME`、`~/.codex/` 以及 Orca 多账户目录）。
- 提供 `scripts/embed_pptx.py` 辅助脚本，支持将生成的整页图片无缝贴合拼入 PPTX 文档并自动清空占位符。
- 编写完整的 `SKILL.md` 与多场景使用规范（文生图、图生图、PPT架构图、产品UI图）。
- 新增单元测试 `tests/test_gen.py` 并通过回归验证。
