---
name: patent-combo
description: "v1.1.0. 代码专利挖掘组合拳编排器：对本地代码库执行「脱敏门禁→rubric 挖点→交底书生成→权利要求撰写→CNIPA+dev-tool 双路查新」四阶段流水线，产出候选清单、交底书、权利要求与查新报告。当用户说「组合拳」「专利组合拳」「挖专利」「专利挖掘」「从代码挖专利」「patent-combo」并指向某个本地代码库/仓库时触发。编排复用实例声明的外部三件套（交底 Skill、权要指南、查新 CLI），不重复造轮子。"
metadata:
  version: "1.1.0"
---

# 代码专利挖掘组合拳 (patent-combo)

四个阶段，每阶段结束向用户汇报并确认后再进入下一阶段。**全程描述性、不下法律结论**（不说"可专利/新颖"，只刻画技术贡献；最终以专利代理人复核为准）。

## 实例配置（先读）

依赖路径一律外置于工作区实例声明，本 Skill 零路径硬编码：

```bash
cat "<工作区根>/.data/patent_combo_config.json"
```

| 配置键 | 含义 | 用途阶段 |
|---|---|---|
| `disclosure_skill` | 交底 Skill 仓库根（含 `skills/patent-disclosure/`） | Stage 2/4 |
| `claims_guide` | 权要撰写指南（PATENT_SKILL.md 全路径） | Stage 3 |
| `priorart_cli` | dev-tool 查重 CLI（默认 `patent`） | Stage 4 |
| `output_root` | 交底书草稿输出根目录 | 收尾 |

配置缺失或键值路径不存在时：❌ 停止执行并列出缺失项；👉 按 `.data/patent_combo_config.json` 模板补齐后重试，不得猜测路径。

## Stage 0 · 脱敏门禁（不可跳过）

工作区硬约束：**未脱敏代码、商业秘密、患者数据严禁进入外部模型上下文**。对目标仓库：
1. 扫描：`.env*`、密钥/证书、患者/临床字样、`secret|password|token|api_key` 命中统计；
2. 默认只向上下文送**代码结构摘要**（目录树、README/DESIGN、核心模块签名与注释、关键算法片段 ≤50 行/处），不粘贴全量源码；
3. 命中敏感项 → 列清单请用户逐项确认「可入上下文 / 需脱敏替换 / 跳过」后方可继续。

## Stage 1 · 挖点

读本目录 `references/mining-rubric.md`（四维评分 0–13，≥8 入选；发明藏在哪里/跳过什么）→ 扫描产出 `01_候选清单.md`（评分、代码证据、counter-case、git blame 发明人归因、披露时序）→ **停下来让用户勾选**要推进的候选，并按清单建议追问 know-how。

## Stage 2 · 交底书

读 `<disclosure_skill>/skills/patent-disclosure/SKILL.md`，从其 Step 3（挖点深化）衔接：已有候选清单时跳过 intake/project_scan 重复采集，直接按 `prompts/invention/`（或 utility_model/design）深化 → `disclosure_preview` → `disclosure_builder` → `disclosure_self_check`，产出 `02_交底书.md`。Word 导出用其 `tools/md_to_docx.py`。

## Stage 3 · 权利要求

读 `<claims_guide>`「八、权利要求书」及实施方式章节，对每份交底：先独权（必要技术特征、最宽合理范围）→ 再从权（层层递进布局）→ 与交底书技术方案交叉核对支撑性。产出 `03_权利要求.md`。

## Stage 4 · 双路查新

1. **CNIPA**：用交底包 `tools/crawl/cnipa_epub_search.py`（两段式：关键词 → IPC 分类号回补，不足 4 条同分类号补第一轮）；Playwright 走系统 Chrome。
2. **dev-tool prior art**：`<priorart_cli> "<用一句话概括的候选点子>"`（19 源语义查重，Open/Crowded/Saturated）。首次运行需下载本地嵌入模型，耗时长属预期。

汇成 `04_查新报告.md`：每候选一节，命中专利列表 + 相似度判断 + 对权要范围的影响建议（描述性措辞）。

## Task Progress（复制到对话逐项打勾）

```markdown
Task Progress:
- [ ] Stage 0: 脱敏门禁扫描与用户确认
- [ ] Stage 1: 挖点候选清单 + 用户勾选
- [ ] Stage 2: 交底书生成（含 self_check）
- [ ] Stage 3: 权利要求撰写
- [ ] Stage 4: 双路查新报告
- [ ] 收尾: 输出目录交付
```

## 收尾

输出至 `<output_root>/{{yyyymmdd}}_{{repo主题}}/`。每份文件末尾追加：

> 本草稿为 AI 辅助挖掘结果，非查新/FTO/法律意见，正式申报前须经专利代理人复核。

完成后向用户报告输出目录与四份文件清单。
