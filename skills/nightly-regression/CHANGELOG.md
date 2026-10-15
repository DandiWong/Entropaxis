# Changelog - nightly-regression Skill

## [1.1.1] - 2026-10-16
### Changed
- 压缩 description 至 ≤250 字符，只保留能力一句与触发词，降低常驻元数据开销。

## [1.1.0] - 2026-10-12
### Added
- 作业注册与管理节（1.5）：本地作业实例经 `tools/nightly_registry.py` 登记到
  `data/templates/nightly.yaml`，支持 list / status / add / enable / disable / remove。
- `status` 三方对齐探测（注册表 × launchd loaded × 最近报告），回答「配了但没生效」。
- B 方案固化「先登记后注册」顺序，交付后须 `status` 复核 plist / loaded / enabled 三态。

### Changed
- 作业实例从 Skill 叙述改为数据驱动：Skill 只做编排，实例与凭据留在 `data/`，
  不随分发包外泄。

## [1.0.0] - 2026-10-12
### Added
- 首次发布：nightly 回归编排执行体，承接《软件工程》「测试与验证」延迟层契约。
- 分层判据（该进/不得进 nightly）、两种承载形态（CI 定时任务优先、本机 launchd 过渡）、
  只报不阻断的报告与通知契约、A 就绪后 B 的退出步骤。
