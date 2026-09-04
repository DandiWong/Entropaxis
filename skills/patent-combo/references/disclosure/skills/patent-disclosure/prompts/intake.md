# 边界与输入录入（Step 1）

## 用途

自动从已有材料提取技术主题、专利类型与文头占位信息；不以提问或用户确认阻断后续流程。

## 自动提取与路由

1. 用户已明确指定专利类型时，按其指定类型执行。
2. 未指定时，按材料信号自动选择：
   - 仅以可见形状、图案、色彩或造型比例为核心，且无关键功能/构造改进 → `外观设计`；
   - 以部件形状、连接关系、空间布局或装配构造为核心，且无以算法/方法步骤为主的技术方案 → `实用新型`；
   - 其余情形、信号冲突或证据不足 → `发明`。
3. 自动写入 `专利类型：<自动判定类型>` 与判定依据；不反问、不等待确认。技术联系人缺失时统一填「待填写」。

## 后续特化

| 专利类型 | 后续特化 |
|----------|----------|
| 发明 | `skills/patent-disclosure/prompts/invention/` |
| 实用新型 | `skills/patent-disclosure/prompts/utility_model/` + `prompts/fill_structure_schema.md`（含 **`figure_plan.yaml`**） |
| 外观设计 | `skills/patent-disclosure/prompts/design/` + `prompts/fill_appearance_schema.md`（含 **`figure_plan.yaml`**） |

查新时把类型传给检索工具：`--type invention|utility_model|design`（见 `prior_art_search.md`）。
