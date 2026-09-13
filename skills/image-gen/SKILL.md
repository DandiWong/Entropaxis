---
name: image-gen
description: "v1.1.0. 通过本地 Codex CLI（复用 ChatGPT Plus/Pro 订阅额度）原生调用 imagegen / gpt-image-2 生成与编辑高质量栅格图片（PPT整页图示、架构图、产品图、UI Mockup、插画、图标等），无需外部 API Key 或额外计费。当用户需要生图、画图、生成图片、PPT配图、绘制架构图、修改图片、或者要求使用 Codex CLI / imagegen / gpt-image-2 生图时使用，即使未指明 skill 名称也应触发。包含自动图片探测提取与 PPTX 整页无缝贴合工具。"
compatibility: "macOS / Linux, Python 3.10+, Codex CLI (logged in)"
metadata:
  version: "1.1.0"
---

# 🎨 Image Gen — 基于 Codex CLI 订阅额度原生生图

本 Skill 允许任意 Agent 通过本地 `codex` CLI 运行的原生 `imagegen` / `gpt-image-2` 能力进行文生图（Text-to-Image）、图生图（Image-to-Image）与 PPT 整页图示生成，直接消耗用户已登录的 ChatGPT Plus / Pro 订阅额度，**无需配置额外 `OPENAI_API_KEY` 或按量计费 Token**。

---

## 核心特性

1. **订阅额度复用**：直接复用本地 `codex` CLI 已登录的 ChatGPT 会话与配额。
2. **多环境目录自适应**：脚本内建多源图片探测逻辑，自动从 `$CODEX_HOME`、`~/.codex/generated_images/` 及 Orca 动态多账户目录中精准捕获最新产物。
3. **零第三方硬依赖**：核心 `scripts/gen.py` 基于纯 Python 标准库开发，开箱即用。
4. **PPTX 整页拼装套件**：配套 `scripts/embed_pptx.py`，支持将生成的 16:9 高清架构图无缝贴合拼入 PPTX，自动探测版式并清除残留占位符。

---

## 快速调用

### 1. 基础文生图 (Text-to-Image)

```bash
python3 <skill-dir>/scripts/gen.py \
  --prompt "A clean modern medical AI architecture diagram on solid light grey #F2F2F2 background, 3-tier horizontal cards" \
  --out "output/architecture_slide.png" \
  --size "16:9"
```

### 2. 带有参考图的图生图 (Image-to-Image / Style Transfer)

```bash
python3 <skill-dir>/scripts/gen.py \
  --prompt "Keep the 3-tier structure unchanged, update the bottom-right card border with bright yellow #F5DF4A accent" \
  --ref "output/architecture_slide.png" \
  --out "output/architecture_slide_v2.png"
```

### 3. 将图片整页拼装至 PPTX (Full-page Slide)

```bash
python3 <skill-dir>/scripts/embed_pptx.py \
  --image "output/architecture_slide.png" \
  --pptx "output/presentation.pptx"
```

---

## CLI 参数完整说明 (`scripts/gen.py`)

| 参数 | 缩写 | 默认值 | 说明 |
|---|---|---|---|
| `--prompt` | `-p` | **必填** | 生图提示词（建议使用结构化规范描述） |
| `--out` | `-o` | **必填** | 输出图片保存路径（如 `output/hero.png`） |
| `--size` | `-s` | `16:9` | 画幅尺寸或比例（如 `2560x1440`、`1024x1024`、`16:9`、`4:3` 等） |
| `--ref` | `-r` | `[]` | 参考图片路径（可重复传入多个 `--ref`） |
| `--timeout` | `-t` | `240` | 超时时间（秒） |
| `--verbose` | `-v` | `False` | 是否打印探测与执行调试日志 |

---

## 最佳提示词结构指南

生成专业技术架构图、PPT整页配图或产品原型时，推荐遵循以下结构化 Prompt 规范：

```text
Use case: productivity-visual
Asset type: full-page 16:9 widescreen presentation slide diagram
Scene/backdrop: solid uniform light grey background #F2F2F2, perfectly flat, no gradient, no border.
Subject: Professional 3-tier architecture diagram with rounded rectangular cards.
Composition/framing: 16:9 widescreen landscape (2560x1440), centered.
Style/medium: clean modern enterprise tech UI diagram, flat design, crisp typography.
Color palette: primary background #F2F2F2, cards neutral white/light grey, accent #F5DF4A yellow strictly on designated items.
Text content (verbatim, exact Chinese characters):
1. Top: 【模块标题】
2. Middle: 「子系统 A」「子系统 B」
3. Bottom: 「底座能力（本季度新增）」
Constraints: exact text rendering, no garbled letters, no watermark.
```

---

## 异常排查

1. **未找到 codex CLI (`exit code 3`)**：
   - 检查系统是否已安装 Codex：`which codex`；
   - 执行 `codex login` 确保已成功登录 ChatGPT 订阅账户。
2. **生图超时 (`exit code 1`)**：
   - 复杂提示词可能耗时 60~120 秒，可适当增大 `--timeout 300`。
