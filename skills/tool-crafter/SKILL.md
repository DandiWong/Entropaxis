---
name: tool-crafter
description: "v1.0.0. 机制工具化与高级 Agent 工具研发套件（基于 ApX 最佳实践与 Entropaxis 工具治理体系）。当用户需要将工作区高频机制、复合命令、脚手架或数据校验逻辑转化为标准化 Python 工具，或要求“机制转工具”、“制作工具”、“创建工具”、“tool-crafter”、“toolify”、“机制工具化”时触发。全自动执行五维准入判定、ApX 接口契约设计（语义化参数、行动导向报错、纯标准库、原子写入）、自动化 TDD 单测生成与双重体检闭环。"
metadata:
  version: "1.0.0"
---

# 🛠️ Tool Crafter — 机制工具化与高级工具研发套件

本 Skill 提供将工作区高频操作、复合 CLI 命令、机械规则计算或破坏性动作**标准化转化为系统级工具 (`.system/tools/<name>.py`)** 的端到端自动化流水线。

核心方法论依据：
- 规则真源：[`../../rules/工具设计.md`](../../rules/工具设计.md)（形态判定见 `知识沉淀.md`）
- 系统演进准则：[`../../rules/01_根系统治理.md`](../../rules/01_根系统治理.md)
- ApX 高级工具工程规范（语义化接口、行动导向错误契约、纯标准库、幂等原子暂存）

---

## 1. 触发条件与场景

当收到以下意图时，必须唤起本 Skill 开展机制工具化：
- “把这个机制转成 tool” / “机制转工具” / “机制工具化”
- “制作工具” / “创建系统工具” / “新建 tool”
- “分析当前机制并提取为工具”
- 输入指令 `tool-crafter` 或 `toolify`

---

## 2. 工具化 6 步标准流水线

```text
Phase 1: 机制准入审查 (Funnel)   ➔ 核对《工具设计》五维判据（确定性/频次/安全/ROI/零绑定）
Phase 2: 接口与契约设计 (Spec)  ➔ 确定动词_名词、参数类型清洗、结构化输出与行动导向报错
Phase 3: 纯标准库代码编写 (Code) ➔ 基于 templates/tool.template.py 生成 .system/tools/<name>.py
Phase 4: 自动化单测驱动 (TDD)    ➔ 编写 .system/tests/test_<name>.py（覆盖正常与边界拦截）
Phase 5: 双重体检门禁 (Verify)  ➔ 跑通 unittest (100%) 与 lint_workspace.py (全绿)
Phase 6: 路由装配与五维评估 (Done)➔ 挂载使用说明至 .data/rules/tips.md，输出五维量化评估表
```

---

## 3. 详细执行 SOP

### Phase 1 · 机制准入审查 (Funnel Check)
在写任何代码前，先对照 [`.system/rules/工具设计.md`](../../rules/工具设计.md) 第 2 节逐项检查：
1. **确定性**：逻辑是否具备确定性输入输出（非主观润色/文本发散）？
2. **复用频次**：是否跨项目复用或为日常核心高频操作？
3. **安全收敛**：能否将原本开放的任意 Shell 收敛为受控参数？
4. **Token ROI**：工具化能否显著减少 LLM 的 CoT 思考 Token 消耗？
5. **零系统绑定**：是否完全不依赖外部私有商业 SaaS 的私有端点？

*若不满足上述条件，向用户明确说明不建议工具化的理由，并建议转为 Rule 约束或 Skill 流程。*

### Phase 2 · 接口与契约设计 (Spec)
1. **命名**：`动词_名词.py`（小写下划线，如 `verify_schema.py`、`export_metrics.py`）；
2. **参数**：使用 `argparse` 定义必选位置参数与可选 `--flag`，支持 `--json` 输出；
3. **行动导向报错**：设计 `ToolError` 异常文案，格式必须包含：
   - `❌ 错误原因: ...`
   - `👉 修复建议: ...（指导 Agent 如何修改参数或修复环境）`

### Phase 3 · 代码编写 (Code Generation)
参考 `templates/tool.template.py` 编写 `.system/tools/<name>.py`：
- **纯 Python 标准库**：严格禁止 `import` 第三方未内置包；
- **零系统绑定**：严禁出现业务机构名、测试环境 IP/本地主机地址或私有密钥；
- **原子暂存**：涉及文件写入时，必须在 `tempfile.TemporaryDirectory` 中生成后原子替换。

### Phase 4 · 自动化单测编写 (TDD)
在 `.system/tests/test_<name>.py` 中编写标准 `unittest.TestCase`：
- 测试正常执行路径与输出结果；
- 测试非法输入时是否抛出带修复指引的 `ToolError`；
- 测试 `--json` 格式解析是否合规。

### Phase 5 · 双重体检门禁 (Dual Gate)
在终端执行双重验证：
```bash
python3 -m unittest discover -s .system/tests
python3 .system/tools/lint_workspace.py
```
必须确保单元测试 100% 通过且 15 项工作区体检全绿。

### Phase 6 · 路由装配与五维架构评估 (Wire & Deliver)
1. 在 `.data/rules/tips.md` 追加该工具的操作提示（`- TIP：...`）；
2. 依据 [`.system/rules/五维评估.md`](../../rules/五维评估.md) 输出标准 4 列五维评估表交付成果。
