---
name: biomedical-style-transfer
description: "Transforms existing diagrams into 2.5D biomedical illustrations through local Codex with ChatGPT subscription access and GPT Image 2.5, preserving source content. Applies to biomedical infographic restyling, 把图转成医学插画风格, 渐变立体医学插画 and 药物机制图风格 requests."
metadata:
  version: "1.1.0"
---

# 生物医学插画风格转换

将其他画风的原图转换为白底、渐变立体、圆润分子造型的生物医学信息图。属于图像编辑；只迁移视觉语言，不迁移参考图的科学内容。

## 输入与默认值

- 必需：一张待转换原图。没有原图时请求提供，不把内置风格参考当作待转换对象。
- 风格参考：[references/style-reference.png](references/style-reference.png)。每次首次生成前查看它；出处与用途见 [references/source.md](references/source.md)。用户另给目标画风时以用户为准。
- 默认保留原语言、全部文字、分区、相对位置、画幅比例与完整内容；输出一张 PNG 草稿。
- 允许用户指定更简洁的期刊版、较饱满的学术交流版、尺寸或重新布局。未指定时匹配参考图的适度高光与渐变，不强制三段说明栏、纵向通路或环形细胞周期。
- 不向用户重复询问已明确的输入或一般审美选择。只有无法辨认的关键文字、关系或来源角色冲突时才澄清。

## 工作流

- [ ] 读取原图与风格参考，区分两者角色。
- [ ] 锁定内容清单、关系与图例含义。
- [ ] 使用图像编辑能力完成转换。
- [ ] 对照原图检查并交付实际产物。

### 1. 锁定原图语义

先查看本地图像，再提取一份紧凑的文字与结构清单到本轮工作上下文；无需另建报告。

- 逐字记录标题、标签、图注、数字、单位、大小写、上下标、希腊字母和分面编号。
- 记录实体、分组与容器边界；逐条记录连接的起点、终点、方向、箭头或 T 型抑制端、实线或虚线及关系标签。
- 保留颜色与图例的对应含义，以及激活态、抑制态、疾病态等已有区别。原图已有语义色时优先保留，不套用参考配色导致含义互换。
- 保留证据强度：假说、间接作用、物理结合、磷酸化和因果作用不可互换；虚线不能因美化变成实线。
- 不能读清时不猜测。若只是非关键装饰可继续；关键内容先请求高清图或文字补充。
- 对非生物学的结构图，只借用材质、字体与连接线风格；不能把服务器、机构或步骤擅自改成细胞、蛋白或疾病机制。
- 显微照片、病理图像、影像检查和定量图表若是研究证据，不通过重绘改变数据或像素证据；只处理独立示意区域。用户明确要教学插画时可另作示意图，并注明不能作为原始实验图使用。
- 原图是转绘依据，不等于科学正确性已经核实。疑似科学错误单独告知，未经用户要求不暗改。

### 2. 应用目标风格

- 白色背景、充足留白、清晰层级，整体为矢量插画观感的 2.5D 科学示意图，而非照片或真实分子结构渲染。
- 原图中已有的蛋白可呈圆润、不规则团块；已有细胞可用简化膜、胞质、核表达。不凭空增加生物结构或分子细节。
- 局部颜色渐变、左上方柔和高光、轻薄投影和清晰轮廓；避免塑料玩具质感、强烈光晕、厚重阴影干扰文字。
- 无语义色约束时，可用深海军蓝文字、青蓝主要结构、珊瑚红上游节点、洋红干预对象、灰色受抑状态。颜色只在原图存在相应角色时使用。
- 无衬线字体、醒目但不过度压缩的标题；标签保持水平可读，连接线避开文字与实体内部。T 型端、双向箭头、虚线按原图逐一保留。
- 无新增标语、靶心图标、编号步骤、化学结构、磷酸化标志、结论或装饰性机制节点。

### 3. 执行编辑

**固定生成路径：本机 Codex CLI + 本机已登录的 ChatGPT 订阅额度 + GPT Image 2.5。** 本任务默认选择侧重精确编辑的 `gpt-image-2.5-sunburst`；用户另选 2.5 系列型号时遵从用户。型号依据与执行限制见 [references/model-route.md](references/model-route.md)，首次执行时读取。

先运行 `codex --version`、`codex login status`、`codex exec --help`，确认 CLI 存在且登录方式为 ChatGPT。不读取或回显凭据，不更换账号，不修改全局配置；不使用 API Key、第三方转发或当前宿主的其他生图路径。本机 CLI 发起请求，图像在服务端生成，不是本地离线推理。

随后核实当前版本是否能为其原生图像工具选择目标图像模型，或有可信工具契约明确将该工具固定映射到目标型号。只使用实际存在且已核实的参数或映射；提示词里写模型名不能证明选中了模型，`codex exec -m` 是执行任务的 Agent 模型参数，不得据此伪造图像模型选择。若工具未暴露选择方式，也无可信映射，停止生图，明确报告“本机订阅路径可用，但 GPT Image 2.5 型号未能确认”，不降级试画。

模型路径核实后，通过本机 `codex exec --enable image_generation` 调用原生图像工具，保留宿主权限边界。可复用已安装的 CLI 生图技能或脚本，但必须先查看它是否真正支持目标图像模型；名称含 `gpt-image-2` 的旧包装器不构成 2.5 支持证据。无合适包装器时直接调用 CLI，不新建 API 客户端。

本地原图和参考图均先查看，再通过 CLI 的 `-i` 分别传入；只有对话附件时先取得其真实本地文件。参考路径相对于本 Skill 目录解析。完整提示词通过标准输入传入，避免图片参数吞掉提示词或 shell 展开。输出仅从本次调用返回的文件路径或明确关联的会话获取，不按全局“最新图片”猜测结果。工具返回型号信息时核对是否匹配；不匹配的产物不能作为合格结果交付。

提示词中明确每张图的角色与顺序，并填入实际语义清单；以下是结构模板，不要把占位符发给工具：

```text
Task: style-transfer edit of an existing scientific diagram.
Execution: local Codex with ChatGPT subscription access only.
Required image model: gpt-image-2.5-sunburst (GPT Image 2.5).
Use the verified image-tool model selection or mapping; stop if unavailable.
CONTENT SOURCE: [identify the source image]. It alone defines all text,
entities, grouping, biological claims and connections.
STYLE REFERENCE: [identify the reference image]. Borrow only its visual
treatment; never copy its labels, proteins, pathways, conclusions or layout.
Locked text, verbatim: [complete source transcription]
Locked entities and relationships: [nodes, compartments, edges, edge types,
directions, legends and uncertainty markers]
Render as a white-background 2.5D biomedical infographic: rounded stylized
forms where appropriate, smooth local gradients, soft upper-left highlights,
subtle shadows, crisp contours, navy sans-serif typography and generous space.
Keep source semantic colors and all scientific symbols. Preserve the source
aspect ratio, panel structure and relative placement unless requested otherwise.
Do not invent biological details, import reference content, omit text, reverse
arrows, turn binding into activation or turn hypotheses into established facts.
Requested adjustments: [actual user changes, or none]
```

路径和型号核实后实际生成图片，不只输出提示词。登录、权限、额度、模型选择或工具调用失败时如实说明，可提供已填好的提示词供后续执行；不得声称已转换，不自动切换到其他型号、API 计费或第三方服务。用户已经选择本机订阅路径，不重复询问路径偏好。

### 4. 检查与修正

打开生成结果，与原图逐项对照清单检查：文字无漏改、实体与分组完整、所有边的起终点和类型正确、颜色图例一致、未混入参考图内容、无裁切或遮挡。需要时放大检查小字与化学符号，不能仅凭缩略图宣布通过。

风格检查：白底、圆润形态、适度渐变高光、清晰轮廓与标签同时成立，不能只把原图换色。

发现差异时针对具体问题编辑，并在每次重试中重申全部不变量。默认最多追加两轮修正；仍不通过则保留草稿，明确列出未解决项，不声称可直接投稿。不能为通过检查而删去困难内容。

栅格编辑不能保证逐字或化学键零误差；不把 PNG 宣称为可编辑 SVG，也不以修改 DPI 元数据冒充新增细节。用户要求精确可编辑文本/矢量时，说明需要保留原生文字和连线层、另行制作插画资产的工作流；仅在用户选择该输出后执行。期刊适配只有在核对具体期刊要求后才能确认。

## 交付与调用

遵循宿主文件交付与命名规则；无相关规则时保存到用户指定位置或独立输出文件夹。保留原文件，结果另存为 `<原图名>_医学插画.png`，重试不覆盖原图。返回实际图片、路径与简短核对结论，并说明本机订阅调用路径及图像型号核实依据；未核实的文字、科学内容和投稿适配状态如实标明。

宿主有统一文件打开入口时，调用并检查其执行结果；缺失时返回绝对路径供用户打开。不得自建另一套打开器或绕过宿主配置。实际失败或降级需告知用户。

调用示例：

> 用 $biomedical-style-transfer 将附件改成渐变立体生物医学插画风格，保留全部英文、箭头和布局。

> 用 $biomedical-style-transfer 转换这张通路图，减少阴影，让它更适合期刊阅读。
