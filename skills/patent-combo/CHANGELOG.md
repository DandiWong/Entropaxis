# CHANGELOG

## [1.4.4] - 2026-09-04

### Changed
- 移除阶段确认、敏感项逐项裁决、出网关键词确认和交底摘要确认；商业敏感项本地隔离，其余敏感内容自动脱敏，检索词自动净化。
- 未指定专利类型时按材料信号自动路由：纯外观 → 外观设计、纯构造 → 实用新型、其余或冲突 → 发明；高分候选不再限制默认仅产出一篇。

## [1.4.3] - 2026-09-04

### Changed
- Stage 1 改为内置四维 rubric（总分 0–12）自动路由：得分 ≥10 的候选全部进入撰写与输出，不再等待人工候选裁决；无入选候选时结束本轮。
- 多个自动入选候选分别在独立事件目录内顺序完成 Stage 2–4，避免最终 `03_查新报告.docx` 命名冲突。

## [1.4.2] - 2026-09-04

### Changed
- 阶段 Markdown 明确为未打开、未展示、未人工审阅的瞬态转换底稿；完成查新后立即收敛为三份 DOCX，且只打开最终 DOCX。

## [1.4.1] - 2026-09-04

### Removed
- 移除 Python 字节码缓存、未使用的 Node/mmdc 遗留清单与教学 STEP 生成器；删除指向未随包分发教学样例的说明，缩小可分发包且不改变运行路径。

## [1.4.0] - 2026-09-04

### Changed
- 最终交付统一收敛为 `01_交底书_<案件名称>.docx`、`02_权利要求_<案件名称>.docx` 与 `03_查新报告.docx`；阶段 Markdown 仅作内部底稿，三份 DOCX 全部成功后才清理。
- 查新报告最终 Word 精简为从「命中专利列表」起、至「人工检索式清单」前止的内容；新增收敛器拒绝覆盖既有交付，并在任一导出失败时保留全部底稿。

## [1.3.2] - 2026-09-04

### Changed
- 删除可分发 Skill 中对 `.data/registry.md` 的间接定位；内置资产出处仅保留随包可验证事实，内部源定位改留受控维护记录。

## [1.3.1] - 2026-09-04

### Changed
- 内置资产出处泛化：可分发面不再携带工作区内部目录结构，源仓库路径改由 `.data/registry.md`（`ip` 项目）间接定位。配套根系统铁律 6 澄清（公共权威数据源/开源许可署名/开源致谢/公共规范引用豁免）。

## [1.3.0] - 2026-09-04

### Added
- 局部安全补丁（随包维护，与上游差异已登记）：`formula_eval.py` `_ast_allowed` 增加常量数值类型守卫，封堵字符串常量经乘法的内存放大向量。

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
