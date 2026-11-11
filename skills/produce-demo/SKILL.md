---
name: produce-demo
description: "以已确认的需求清单为输入出具高保真单文件 HTML 交互原型。包含 IPO 三栏步进流水线、初始空态、点击 Mock 切换、630px 宽屏 Output、通俗人话文案与体检门禁。触发词：出demo、做demo、设计demo、出交互原型、做原型。"
metadata:
  scope: control-plane
  version: "1.0.0"
---

# Produce Demo — 高保真交互 Demo 设计与交付

本 Skill 承接已确认的 `01_需求清单.md`（或 `*_reqs.json`），出具免构建、自解释、符合用户交互美学的高保真单文件 HTML 原型。

**独立性契约**：本指令可独立调用，前置需求清单缺失时自动提示用户先执行「提取需求」或提供输入；产出后执行体检并唤起浏览器确认。

规范细节见 [`references/ipo-demo-spec.md`](references/ipo-demo-spec.md)；脚手架与体检工具见 `scaffold_demo.py`。

## 进度清单

复制到对话逐项打勾：

- [ ] 1 读取需求清单（定位 `01_需求清单.md` 或 `*_reqs.json`，提取阶段结构）
- [ ] 2 IPO 流水线脚手架（调 `scaffold_demo.py --from-reqs` 生成骨架）
- [ ] 3 业务字段与交互呈现（调度 Designer 完善控件与动作，拒绝大段解释词）
- [ ] 4 极致体验自查（初始空态、点击Mock切换、630px宽屏呼吸感、通俗人话文案）
- [ ] 5 规范体检与交付（执行 `scaffold_demo.py --lint`，调用 `open_file.py --on-deliver` 打开）

---

## 核心设计与交互规范

### 1 核心原则：用控件与交互呈现产品，拒绝加一堆解释词
杜绝纯文本说明装饰块，用**表单控件、拖拽上传虚线框、点击徽标、状态开关与 Diff 对比框**让用户直观感知功能。

### 2 线性步进流水线与 IPO 三栏架构
- **顶部 Stepper**：显式步进导航，初始仅阶段 1 激活，后续阶段置灰锁定（`locked`）；
- **IPO 三栏栅格**：严格采用 `grid-template-columns: 280px minmax(340px, 1fr) 630px;`（左 Input、中 Process、右 Output 宽屏）；
- **上下游流水线继承**：完成上阶段后解锁下阶段，下阶段 Input 显式展示绿色继承提示框（`✔ 已继承上一阶段产出成果...`）。

### 3 演示交互细节
1. **初始空态占位（Empty State）**：中栏与右栏初始呈现干净的空态卡片，杜绝首屏杂乱数据。
2. **点击 Mock 快速切换（演示利器）**：输入端放置虚线卡片，用户轻点卡片即刻切换为带绿对勾的“脱敏样本已就绪”状态。
3. **文案讲一线人话**：
   - 拒绝生硬研发黑话（如将“触发自动下发的 Query 提示语”改写为“录入报错提示语（数据录错时，弹给医生看的话）”）；
   - 逻辑表达式采用等宽字体与深灰背景，报错提示采用暖黄气泡卡片。
4. **版本保护铁律**：激进重构原型前，必须就地备份稳定底稿（如 `index.backup_YYYYMMDD.html`）。

### 4 执行命令与工具
1. **骨架生成**：
   ```bash
   python3 .entropaxis/tools/scaffold_demo.py --from-reqs <reqs.json> --out <demo.html>
   # 或自定义阶段：
   python3 .entropaxis/tools/scaffold_demo.py --title "<项目>交互Demo" --stages "阶段1,阶段2,阶段3,阶段4" --out <demo.html>
   ```
2. **自动化规范体检**：
   ```bash
   python3 .entropaxis/tools/scaffold_demo.py --lint <demo.html>
   ```
3. **物理打开闭环**：
   ```bash
   python3 .entropaxis/tools/open_file.py --on-deliver <demo.html>
   ```
