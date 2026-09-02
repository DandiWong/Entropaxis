---
name: ppt-design
description: "v1.0.0. 极简高管汇报级 PPT 设计规范与原生生成工具包。当用户提到“整理为 PPT”、“制作 PPT”、“生成汇报 PPT”、“生成 PPT”、“做个 PPT”、“做 PPT”、“PPT 汇报”、“优化 PPT”、“汇报 PPT”等任何 PPT 制作、整理或重构需求时，必须默认使用本 Skill。专注于去边框、去装饰线、纯粹基于字阶（字号/字重/颜色对比）、呼吸留白与精准栅格对齐的现代极简排版风格。内置标准 16:9 页面配方（3-KPI 现状页、3-Pillar 定位页、6-Stage 箭头流转流水线页、4-Value 价值与决策页）与开箱即用的 python-pptx 原生生成器。"
compatibility: "Python 3.10+, python-pptx"
metadata:
  version: "1.0.0"
---

# PPT 极简高管设计规范与生成套件 (ppt-design)

本 Skill 沉淀了一套**专为面向领导/高管汇报设计的现代极简排版范式**与自动化生成工具。

核心设计哲学：**“无框无界，字阶定秩序；结论先行，对齐见专业。”**

---

## 1. 触发规则与应用场景

当收到以下意图时，**默认全部采用本 Skill 规范与模板**生成 PPT：
- “整理为 PPT”
- “制作 PPT”
- “生成汇报 PPT”
- “生成 PPT”
- “做个 PPT” / “做 PPT” / “PPT 汇报” / “优化 PPT”

---

## 2. 核心设计哲学与四大铁律

1. **去线条去边框化 (No Border, No Box)**：
   - 彻底废除所有生硬的卡片描边矩形、灰色包裹底块与繁复网格线；
   - 依靠字阶差异（46pt → 28pt → 17pt → 14pt → 12pt）、字重强弱（Bold vs Regular）与精准的 X/Y 坐标对齐划分视觉空间。
2. **结论先行与高管视角 (Executive-First)**：
   - 顶部永远具备小字号分类定位标签（`13pt Bold Accent Blue`），让领导一秒看懂“这一页在整体叙事中的位置”；
   - 主标题必须是“结论型判断句”（`28~32pt Bold Deep Blue`），绝不用干瘪的名词（如“项目背景”）；
   - 副标题承担“核心因果/依据阐释”（`14.5pt Regular Muted Slate`）。
3. **流程导向与极简指引 (Flow-Driven with Subtle Arrows)**：
   - 业务流水线采用水平横向铺开，两两之间用轻量流转符号 `→` 串联，展示工程与业务闭环，拒绝笨重流程图。
4. **决策闭环与明确诉求 (Clear Ask & Governance)**：
   - 页面底部统一安排“机制提炼”、“管理跃迁”或“领导支持诉求 (Ask)”，直接推动高管决策。

---

## 3. 视觉色彩系统 (Color Tokens)

标准 16:9 画布（宽 13.333 英寸，高 7.5 英寸 / 12192000 x 6858000 EMU）：

| 变量名称 | 色值代码 | RGB 对应 | 用途场景 |
| :--- | :--- | :--- | :--- |
| **`C_DEEP_BLUE`** | `#1B365D` | `(27, 54, 93)` | **主标题、一级重点标题、核心数字** |
| **`C_ACCENT_BLUE`** | `#0A3A76` | `(10, 58, 118)` | **顶部定位 Tag、栏目序号（01/02）、流程副标** |
| **`C_ORANGE`** | `#C2410C` | `(194, 65, 12)` | **痛点数据、极致提效指标对比高亮（如 1个月→1小时）** |
| **`C_ARROW`** | `#0E7490` | `(14, 116, 144)` | **业务流水线流转箭头 `→`** |
| **`C_TEXT_MAIN`** | `#1F2937` | `(31, 41, 55)` | **二级标题、指标名称、加粗前缀** |
| **`C_TEXT_BODY`** | `#374151` | `(55, 65, 81)` | **正文描述段落、列表详细说明** |
| **`C_TEXT_MUTED`** | `#64748B` | `(100, 116, 139)` | **页副标题、次级辅助说明、细则注释** |
| **`C_BG`** | `#FFFFFF` | `(255, 255, 255)` | **画布纯净底色（无底色污染）** |

---

## 4. 标准字阶与栅格系统 (Typography Scale)

推荐字体：`PingFang SC`（macOS/全平台首选）/ `Heiti SC` / `Microsoft YaHei`

```text
[Header 区域]
├── Category Tag   : 13 pt | Bold | C_ACCENT_BLUE (y: 0.45")
├── Main Title     : 28 ~ 32 pt | Bold | C_DEEP_BLUE (y: 0.80")
└── Subtitle       : 14.5 ~ 15 pt | Regular | C_TEXT_MUTED (y: 1.45")

[Body 核心内容区 (y: 2.25" ~ 4.85")]
├── Hero Number    : 44 ~ 48 pt | Bold | C_DEEP_BLUE / C_ORANGE
├── Number Unit    : 18 ~ 20 pt | Bold
├── Column Header  : 16 ~ 18 pt | Bold | C_DEEP_BLUE
├── Column Body    : 13 ~ 14.5 pt | Regular | C_TEXT_BODY
└── Sub-bullets    : 11.5 ~ 12.5 pt | Regular | C_TEXT_BODY

[Footer 底部收尾区 (y: 5.15" ~ 6.85")]
├── Bottom Tag     : 14 pt | Bold | C_ACCENT_BLUE
└── Bottom Content : 13.5 ~ 14.5 pt | Regular / Bold | C_TEXT_BODY / C_TEXT_MAIN
```

---

## 5. 四大标准页面排版配方 (Layout Recipes)

### 配方 1：3-Metric 现状基线与痛点页
* **结构**：顶部 Header + 中部 3 列等宽指标 + 底部深层痛点归因
* **指标列组成**：
  * 大数字（46pt）+ 单位（20pt）
  * 核心指标名（16pt Bold）
  * 两行客观现状与损耗说明（13pt Muted）
* **底部**：深层业务痛点分析（小标题 + 完整逻辑推论）

### 配方 2：3-Pillar 业务定位与解法页
* **结构**：顶部 Header + 中部 3 列核心原则 + 底部管理范式跃迁
* **原则列组成**：
  * 序列号 `01 / 02 / 03`（26pt Bold Accent Blue）
  * 原则大标题（17pt Bold Deep Blue）
  * 完整逻辑解析段落（13.5pt Regular）
* **底部**：管理范式升级陈述（如打破黑盒、统筹闭环）

### 配方 3：6-Stage 业务流水线与流转箭头页
* **结构**：顶部 Header + 中部 6 阶段水平流转（带 `→`） + 底部核心风控与治理机制
* **6 阶段水平栅格**：
  * 阶段宽：`1.68"`，箭头宽：`0.32"`，左边距：`0.8"`
  * 阶段标号与名称：`① 材料解析`（15.5pt Bold Deep Blue）
  * 阶段副标：`确定性信息抽取`（12pt Bold Accent Blue）
  * 核心要点：3~4 行精炼 Bullet Points（11.5pt Regular）
  * 流转连接符：`→`（20pt Bold C_ARROW，垂直居中）
* **底部**：3 条核心风控铁律（事实源锁定、独立质控隔离、人类专家终审）

### 配方 4：4-Value 价值对照与落地决策页
* **结构**：顶部 Header + 中部 4 列价值卡片（速度/质量/治理/复用） + 底部推进计划与 Ask
* **4 列价值组成**：
  * 维度 Tag（13pt Bold Accent Blue）
  * 提效对比数据（20pt Bold，如 `1 个月 → 1 小时` 橙色高亮）
  * 核心价值名（14pt Bold）
  * 收益详述（12pt Muted）
* **底部**：
  * 3 步试点推进计划（选定样本 → 联合评审 → 确立决策）
  * 领导支持请求（`提请领导支持事项：...` 14pt Bold Deep Blue）

---

## 6. 开箱即用 Python-pptx 模板代码

完整可执行代码位于 `templates/generate_deck.py`。
使用示例：

```bash
python3 .system/skills/ppt-design/templates/generate_deck.py --output "output/my_presentation.pptx"
```

---

## 7. 交付收尾刚性门禁 (Mandatory Delivery Gate)

遵循 [`.system/rules/文件交付.md`](../../rules/文件交付.md)：
* **落盘后必开文件**：生成 `.pptx` 文件及相关配图后，在任务结束交付前，必须调用系统默认程序打开生成的 `.pptx` 文件：
  * macOS：`open "<file_path>"`
  * Windows：`start "" "<file_path>"`
  * Linux：`xdg-open "<file_path>"`
