# Changelog

All notable changes to the `tool-crafter` skill will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

## [2.1.0] - 2026-09-27

### 变更
- 更名为 `system-evolve`。
- 删除"结果是否一致"问题：一致性对任何需求都是硬要求，不区分形态；改由 Agent 从现场自判"能否写成固定代码"。
- 用户要求不提问或整体盘点时，跳过问答，按现场事实自判并标明推断。

## [2.0.0] - 2026-09-27

### 变更
- 由 `tool-crafter` 更名为 `evolve-crafter`，范围从"机制转工具"扩大到控制面规则、工具、Skill 的全部设计级增删改查。
- 新增"读现场 → 人话问答 → 判定单确认"前置流程，按判定结果分派 Rule / Tool / Skill / 删除四个落地分支。
- Skill 创建并入本 Skill，不再只有规则里的触发词、没有执行体。

## [1.0.0] - 2026-09-03

### 新增
- 首次发布 `tool-crafter` Skill，支持基于 ApX 工程规范与 Entropaxis 工具治理体系将工作区机制自动化转化为标准系统工具。
- 内置 `templates/tool.template.py` 标准库代码脚手架与 6 步自迭代闭环流水线。
