---
name: patent-combo
description: "v1.4.5. 专利组合拳（代码专利挖掘自包含技能包）：内置交底技能、权利要求撰写指南与 CNIPA 检索脚本三件套，对本地代码库执行全自动「脱敏→评分挖点→交底书→权利要求→CNIPA+开发者工具源查新」流水线；内置 rubric 得分 ≥10 的候选自动进入撰写并直接交付 DOCX，不等待人工候选裁决。发明交底强制包含系统框图、流程图及脱敏关键实现代码。仅 STEP 解析安装、公式 PNG 安装及外观分案保留人工确认。当用户说「组合拳」「专利组合拳」「挖专利」「专利挖掘」「从代码挖专利」「patent-combo」并指向某个本地代码库/仓库时触发。"
compatibility: "Python 3.10+；核心流程（Stage 0-3）零第三方依赖；CNIPA 查新需 playwright+系统 Chrome/Edge，DOCX 最终交付需 python-docx/latex2mathml/PyYAML；转换器依赖缺失或导出失败时阻断收尾，不得以 Markdown 替代"
metadata:
  version: "1.4.5"
---

# 代码专利挖掘组合拳 (patent-combo)

**自包含 Skill 包**：交底 Skill、权要指南与 CNIPA 检索脚本已内置 `references/`，随包分发，无外部仓库依赖。除明确保留的安全与安装确认外，四个阶段连续自动执行。**全程描述性、不下法律结论**（不说"可专利/新颖"，只刻画技术贡献；最终以专利代理人复核为准）。

## 环境自检与降级（任何 Stage 之前先跑）

```bash
python3 "<skill-dir>/scripts/check_env.py" --fix
```

- **exit 0**：核心阶段（Stage 0-3）就绪；输出中列出的降级阶段照常推进，并如实告知用户。
- **exit 2**：核心资产缺失（内置包不完整）→ ❌ 阻断，按 👉 提示重新部署 Skill 包，**禁止手工拼接或猜测路径**。

降级矩阵（安装失败时的确定性行为）：

| 失败项 | 降级方案 |
|---|---|
| playwright / 系统浏览器 | Stage 4 CNIPA 路降级为**人工检索包**：输出关键词 + IPC 分类号 + epub.cnipa.gov.cn 检索指引；查新报告「人工检索式清单」注明「未执行（人工）」 |
| prior-art CLI（`patent`） | Stage 4 dev-tool 路降级：报告标注「未执行」，改产一句话检索式清单供人工检索 |
| verdict 后端（ollama / OpenAI 兼容 API） | CLI 存在时自动追加 `--fast --keyword-only` 降级为**搜索级查新**，报告明示「无语义裁决」 |
| `output_root` 不可用 | 产物仅在会话中交付，不落盘 |

## 实例配置（先读）

```bash
cat "<工作区根>/.data/patent_combo_config.json"
```

| 配置键 | 必需 | 含义 |
|---|---|---|
| `output_root` | ✅ | 交底书草稿输出根目录（不存在时 `check_env.py --fix` 自动创建） |
| `priorart_cli` | 可选 | 覆盖 dev-tool CLI 名（默认 `patent`） |
| `disclosure_skill` / `claims_guide` | 可选 | **旧版覆盖键**：指向外部交底 Skill 仓库 / 权要指南时进入 legacy 模式；缺省一律用内置资产 |

配置文件缺失或 JSON 损坏 → ❌ 停止执行，按 👉 提示补建后重试，不得猜测路径。

## Stage 0 · 脱敏门禁（不可跳过）

工作区硬约束：**未脱敏代码、商业秘密、患者数据严禁进入外部模型上下文**。对目标仓库：
1. 扫描：`.env*`、密钥/证书、患者/临床字样、`secret|password|token|api_key` 命中统计；
2. 默认只向上下文送**代码结构摘要**（目录树、README/DESIGN、核心模块签名与注释、关键算法片段 ≤50 行/处），不粘贴全量源码；
3. 商业秘密、未公开业务标识和项目代号保留于本地、排除出模型与出网上下文；凭证、个人信息、患者/临床内容自动脱敏或跳过。完成后直接继续，严禁逐项要求用户裁决；
4. Stage 4 出网检索词自动仅保留技术术语与通用概念，自动剔除项目代号、内部系统名、未公开产品名及其他敏感标识；无需用户确认。

## Stage 1 · 挖点

读本目录 `references/mining-rubric.md`，对每项按四维（总分 0–12）打分。**得分 ≥10 的全部候选自动进入 Stage 2–4 撰写与输出，严禁停下来要求用户勾选、裁决或补充 know-how。**为每个入选候选在 `<output_root>/{{yyyymmdd}}_{{repo主题}}/` 下创建独立的 `{{序号}}_{{规范化案件名称}}/` 事件目录，并逐一完成交底书、权利要求与查新；得分 <10 的候选不进入撰写。无候选达到 10 分时，报告“无自动入选候选”后结束本轮，不生成交底书、权利要求或查新报告。

## Stage 2 · 交底书

读 `<skill-dir>/references/disclosure/skills/patent-disclosure/SKILL.md`（路径映射：该文档内所有 `skills/patent-disclosure/` 前缀对应 `<skill-dir>/references/disclosure/skills/patent-disclosure/`），从其 Step 3（挖点深化）衔接：已有候选清单时跳过 intake/project_scan 重复采集，直接按 `prompts/invention/`（或 utility_model/design）深化 → `disclosure_preview`（内部自动预览）→ `disclosure_builder` → `disclosure_self_check`。发明交底必须按 builder 生成至少两张 fenced Mermaid 图（3.2 系统框图、3.4 系统流程图），并设置“关键实现代码”章节，放入至少一个来自候选代码证据的脱敏代码或伪代码块；每段不超过 50 行，保留决定技术机制的分支、状态转换或数据结构，删除凭证、内部名称、绝对路径与无关样板代码。仅列文件路径不算代码摘录。内部生成 `交底书工作稿.md` 后直接流转至后续阶段和收敛器；**严禁打开、向用户展示或要求人工审阅该 Markdown**，`disclosure_self_check` 仍作为内部自动质量门禁执行。

## Stage 3 · 权利要求

读 `<skill-dir>/references/claims-guide/PATENT_SKILL.md`「八、权利要求书」及实施方式章节，对每份交底：先独权（必要技术特征、最宽合理范围）→ 再从权（层层递进布局）→ 与交底书技术方案交叉核对支撑性。内部生成 `权利要求工作稿.md` 后直接流转至收敛器；**严禁打开、向用户展示或要求人工审阅该 Markdown**。

## Stage 4 · 双路查新

1. **CNIPA**：`python3 "<skill-dir>/references/disclosure/skills/patent-disclosure/tools/crawl/cnipa_epub_search.py" [--type invention|utility_model|design] [--class CODE] <关键词...>`（两段式：关键词 → IPC 分类号回补，不足 4 条同分类号补第一轮）；Playwright 走系统 Chrome。**降级**：环境自检标记缺失时不出脚本，改产人工检索包。
2. **dev-tool prior art**：`<priorart_cli> "<用一句话概括的候选点子>"`（19 源语义查重，Open/Crowded/Saturated）。verdict 后端缺失时自动追加 `--fast --keyword-only`；首次运行需下载 ~80MB 本地嵌入模型，耗时长属预期。

内部生成 `查新报告工作稿.md`。报告必须包含同级章节「命中专利列表」与其后的「人工检索式清单」；直接由收敛器截取前者至后者前的内容生成 DOCX，**严禁打开、向用户展示或要求人工审阅该 Markdown**。

## Task Progress（复制到对话逐项打勾）

```markdown
Task Progress:
- [ ] 环境自检（check_env --fix）与降级确认
- [ ] Stage 0: 自动敏感内容隔离与检索词净化
- [ ] Stage 1: 按 rubric 自动筛选 ≥10 分候选
- [ ] Stage 2: 交底书生成（发明含两张 Mermaid 图、脱敏关键实现代码及 self_check）
- [ ] Stage 3: 权利要求撰写
- [ ] Stage 4: 双路查新报告
- [ ] 收尾: 不打开 Markdown，直接生成三份 DOCX 并清理阶段底稿
```

## 收尾

每个入选候选的事件目录位于 `<output_root>/{{yyyymmdd}}_{{repo主题}}/{{序号}}_{{规范化案件名称}}/`。每份文件末尾追加：

> 本草稿为 AI 辅助挖掘结果，非查新/FTO/法律意见，正式申报前须经专利代理人复核。

每个入选候选完成 Stage 4 后，**不打开、不展示、不要求人工审阅任何阶段 Markdown**，立即在其事件目录使用内置收敛器生成最终 DOCX 并清理阶段底稿。发明交底的收敛器会机械检查两张 Mermaid 图与“关键实现代码”代码块，先调用 `mermaid_render.py` 生成 PNG，再转换 Word；缺项、渲染失败或 PNG 缺失均阻断收尾并保留全部底稿：

```bash
python3 "<skill-dir>/scripts/finalize_outputs.py" \
  --case-dir "<事件目录>" \
  --case-name "<规范化案件名称>" \
  --candidate-md "<事件目录>/01_候选清单.md" \
  --disclosure-md "<事件目录>/交底书工作稿.md" \
  --claims-md "<事件目录>/权利要求工作稿.md" \
  --report-md "<事件目录>/查新报告工作稿.md"
```

成功后，向用户交付且仅报告下列最终文件；收敛器会删除四份阶段 Markdown 底稿：

- `01_交底书_<案件名称>.docx`
- `02_权利要求_<案件名称>.docx`
- `03_查新报告.docx`（内容从「命中专利列表」开始，到「人工检索式清单」之前结束）

收敛器拒绝覆盖已有最终文件；任一 DOCX 导出失败时保留所有 Markdown 底稿并阻断收尾，向用户明示失败原因，严禁以 Markdown 替代交付。成功后仅逐一调用系统默认程序打开三份 DOCX，再报告输出目录、文件清单与降级情况（若有）。

## 内置资产出处

- `references/disclosure/`：内部维护的上游资产快照；MIT License 随附于该目录；
- `references/claims-guide/`：内部维护的上游资产快照；License 随附于该目录。

内部源定位、更新记录与审核信息不属于可分发包，且不构成阅读或运行本 Skill 的前置条件。升级外部源后须同步本包并在此登记日期，禁止两处版本无声漂移。
