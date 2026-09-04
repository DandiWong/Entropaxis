---
name: patent-combo
description: "v1.3.0. 专利组合拳（代码专利挖掘自包含技能包）：内置交底技能、权利要求撰写指南与 CNIPA 检索脚本三件套，对本地代码库执行「脱敏门禁→评分挖点→交底书生成→权利要求撰写→CNIPA+开发者工具源双路查新」流水线，产出候选清单、交底书（仅 docx 交付）、权利要求与查新报告。当用户说「组合拳」「专利组合拳」「挖专利」「专利挖掘」「从代码挖专利」「patent-combo」并指向某个本地代码库/仓库时触发。启动前先运行 scripts/check_env.py 自检依赖（--fix 自动安装），安装失败自动进入降级矩阵（人工检索包/搜索级查新），不阻断核心阶段。"
compatibility: "Python 3.10+；核心流程（Stage 0-3）零第三方依赖；CNIPA 查新需 playwright+系统 Chrome/Edge，Word 导出需 python-docx/latex2mathml/PyYAML（用时自检并 --fix 安装，失败自动降级）"
metadata:
  version: "1.3.0"
---

# 代码专利挖掘组合拳 (patent-combo)

**自包含 Skill 包**：交底 Skill、权要指南与 CNIPA 检索脚本已内置 `references/`，随包分发，无外部仓库依赖。四个阶段，每阶段结束向用户汇报并确认后再进入下一阶段。**全程描述性、不下法律结论**（不说"可专利/新颖"，只刻画技术贡献；最终以专利代理人复核为准）。

## 环境自检与降级（任何 Stage 之前先跑）

```bash
python3 "<skill-dir>/scripts/check_env.py" --fix
```

- **exit 0**：核心阶段（Stage 0-3）就绪；输出中列出的降级阶段照常推进，并如实告知用户。
- **exit 2**：核心资产缺失（内置包不完整）→ ❌ 阻断，按 👉 提示重新部署 Skill 包，**禁止手工拼接或猜测路径**。

降级矩阵（安装失败时的确定性行为）：

| 失败项 | 降级方案 |
|---|---|
| playwright / 系统浏览器 | Stage 4 CNIPA 路降级为**人工检索包**：输出关键词 + IPC 分类号 + epub.cnipa.gov.cn 检索指引；`04_查新报告.md` 该节标注「未执行（人工）」 |
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
3. 命中敏感项 → 列清单请用户逐项确认「可入上下文 / 需脱敏替换 / 跳过」后方可继续。

## Stage 1 · 挖点

读本目录 `references/mining-rubric.md`（四维评分 0–13，≥8 入选；发明藏在哪里/跳过什么）→ 扫描产出 `01_候选清单.md`（评分、代码证据、counter-case、git blame 发明人归因、披露时序）→ **停下来让用户勾选**要推进的候选，并按清单建议追问 know-how。

## Stage 2 · 交底书

读 `<skill-dir>/references/disclosure/skills/patent-disclosure/SKILL.md`（路径映射：该文档内所有 `skills/patent-disclosure/` 前缀对应 `<skill-dir>/references/disclosure/skills/patent-disclosure/`），从其 Step 3（挖点深化）衔接：已有候选清单时跳过 intake/project_scan 重复采集，直接按 `prompts/invention/`（或 utility_model/design）深化 → `disclosure_preview` → `disclosure_builder` → `disclosure_self_check`。**交底书唯一交付格式为 docx**：用其 `tools/md_to_docx.py` 将 `02_交底书.md` 导出为 `02_交底书.docx`；md 与导出过程中的图片/图表仅为中间产物，留作 Stage 3/4 底稿，收尾时统一清理。Word 导出失败时**保留 `02_交底书.md` 并向用户明示**，严禁删除唯一可用格式。

## Stage 3 · 权利要求

读 `<skill-dir>/references/claims-guide/PATENT_SKILL.md`「八、权利要求书」及实施方式章节，对每份交底：先独权（必要技术特征、最宽合理范围）→ 再从权（层层递进布局）→ 与交底书技术方案交叉核对支撑性。产出 `03_权利要求.md`。

## Stage 4 · 双路查新

1. **CNIPA**：`python3 "<skill-dir>/references/disclosure/skills/patent-disclosure/tools/crawl/cnipa_epub_search.py" [--type invention|utility_model|design] [--class CODE] <关键词...>`（两段式：关键词 → IPC 分类号回补，不足 4 条同分类号补第一轮）；Playwright 走系统 Chrome。**降级**：环境自检标记缺失时不出脚本，改产人工检索包。
2. **dev-tool prior art**：`<priorart_cli> "<用一句话概括的候选点子>"`（19 源语义查重，Open/Crowded/Saturated）。verdict 后端缺失时自动追加 `--fast --keyword-only`；首次运行需下载 ~80MB 本地嵌入模型，耗时长属预期。

汇成 `04_查新报告.md`：每候选一节，命中专利列表 + 相似度判断 + 对权要范围的影响建议（描述性措辞）。

## Task Progress（复制到对话逐项打勾）

```markdown
Task Progress:
- [ ] 环境自检（check_env --fix）与降级确认
- [ ] Stage 0: 脱敏门禁扫描与用户确认
- [ ] Stage 1: 挖点候选清单 + 用户勾选
- [ ] Stage 2: 交底书生成（含 self_check）
- [ ] Stage 3: 权利要求撰写
- [ ] Stage 4: 双路查新报告
- [ ] 收尾: 输出目录交付（交底书仅留 docx，清理中间产物）
```

## 收尾

输出至 `<output_root>/{{yyyymmdd}}_{{repo主题}}/`。每份文件末尾追加：

> 本草稿为 AI 辅助挖掘结果，非查新/FTO/法律意见，正式申报前须经专利代理人复核。

**交底书清理（强制）**：全部阶段完成后，删除事件目录中的 `02_交底书.md` 与导出过程产生的中间图片/图表文件，仅保留 `02_交底书.docx`；`01_候选清单.md`、`03_权利要求.md`、`04_查新报告.md` 不在清理范围。Word 导出失败时豁免本条（保留 md 并向用户明示）。

完成后向用户报告输出目录、文件清单、清理结果与降级情况（若有）。

## 内置资产出处（v1.2.0 定版，2026-09-04）

- `references/disclosure/` ← `09知识产权/02_开发/patent-disclosure-skill`（MIT License 随附于该目录）；
- `references/claims-guide/` ← `09知识产权/02_开发/PatentWriterAgent/PATENT_SKILL.md`（License 随附）。

升级外部源后须同步本包并在此登记日期，禁止两处版本无声漂移。
