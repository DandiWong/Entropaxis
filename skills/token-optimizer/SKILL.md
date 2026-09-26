---
name: token-optimizer
description: "v1.0.0. Analyzes, audits, and refactors rules, skills, tools, and prompts for token efficiency, minimum syntax tax, and high information density. Used when optimizing context overhead, restructuring verbose markdown tables into compact key-values, slimming down system rules, evaluating token ROI, or reducing prompt costs across the workspace."
metadata:
  scope: control-plane
  version: "1.0.0"
---

# Token 经济性与语法税优化套件 (Token Optimizer)

将大模型的高阶认知算力聚焦于核心架构设计、对抗审计与代码生成，将所有机械检索、排版解析、路径查找与状态校验以趋近于零的 Token 代价由本地确定性代码承载。

规则真源见 [`../../rules/00_元规则.md`](../../rules/00_元规则.md)「薄入口与 Token 经济性」与 [`../../rules/系统演进准则.md`](../../rules/系统演进准则.md)「五性」。

---

## 1. 核心评估公式

$$\text{Token 收益 (ROI)} = \frac{\text{带来的确定性决策与正确交付}}{\text{消耗的提示词 (Prompt) + 思考 (CoT) + 生成 (Output) Tokens}}$$

---

## 2. Token 经济性六维优化清单

### 1. 静态规则与文档层（语法税瘦身）
- **消除排版语法税**：禁止在核心上下文中使用人类对齐的宽大 Markdown 表格（`|---|---|`）、连续空格与 ASCII 边框线；改用冒号键值行（`key: value [aliases]`）或紧凑 YAML；
- **常驻层严格预算**：根入口 `AGENTS.md` 严格 $\le 50$ 行；按需规则正文严格 $\le 120$ 行；
- **单跳引用铁律**：引用深度严格为 1（`A -> B`），严禁深层递归引用；
- **正交收敛与去重**：同一事实/判据全局唯一物理正文，一律引用不复述。

### 2. 动态上下文与架构层（渐进式披露）
- **三级渐进披露**：L1 元数据探测（~10 tokens）➔ L2 SOP 概览（~200 tokens）➔ L3 领域参考（按需切片加载）；
- **生命周期隔离**：过程态讨论/审计草稿不写回常驻主干，仅拍板基线入 `DECISIONS.md`。

### 3. 计算与工具分工层（Code-over-Prompt）
- **确定性硬算 100% 下沉 Tool**：禁止模型数行数、算哈希、正则扫表或深层递归目录；一律封装为纯标准库本地 Python 工具；
- **极简返回契约**：工具 stdout 默认返回单行高密度纯文本（~15 tokens），杜绝排版废话。

### 4. 数据结构与存储层（结构化与索引）
- **结构化 Sidecar**：机器判定的枚举/指纹/状态拆入独立 JSON/YAML，不寄生在 Markdown 正文；
- **前置索引化**：大文件维护结构化 TOC，利用 `offset/limit` 按节切片读取，禁止全文无脑读。

### 5. 交互与输出层（行动导向）
- **默认行动与静默解析**：能查证的绝不反问，杜绝无意义“确认-等待”交互轮次；
- **克制交付**：直接呈现核心产物与验证结论，严禁寒暄与过程流水账。

### 6. 持久化与记忆层（增量底册）
- **就地原地更新**：方案演进与审计报告原地维护，严禁衍生 `v2.md` 等碎片副本；
- **事实底册化**：跨会话依赖 `DECISIONS.md` 与 `Tasks.md` 恢复全貌。

---

## 3. 标准 4 步重构 SOP

```text
Step 1: 静态审计 ➔ 运行 scripts/token_audit.py 扫描目标文件 Token 估算与语法税占比
Step 2: 逻辑下沉 ➔ 将机械查找、表格过滤或哈希计算提取为纯标准库 Tool
Step 3: 架构切片 ➔ 将大文件改造为 L1 元数据 + L2 极简 SOP + L3 单跳引用切片
Step 4: 双重验证 ➔ 运行 unittest 与 lint_workspace.py 核验预算与全项绿灯
```

---

## 4. 辅助工具用法

在当前 Skill 目录下直接执行静态审计工具：

```bash
# 审计单个规则文件
python3 .entropaxis/skills/token-optimizer/scripts/token_audit.py .entropaxis/rules/工作流指令.md

# 审计整个规则库（支持 JSON 输出）
python3 .entropaxis/skills/token-optimizer/scripts/token_audit.py .entropaxis/rules/ --json
```
