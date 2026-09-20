"""交付物正文结构门禁测试（A）。

调度此前只判「文件写出来了」，《方案调研》的四段式、横向对比与信源标注全靠模型自觉——
实测同一 Researcher 两次运行，一次带 6 条链接、一次 0 条，都算 succeeded。本组证明判据
会在缺失时真的拦住，且不误伤合规产出。
"""

import sys
import tempfile
from pathlib import Path
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

import validate_schema as vs  # noqa: E402

FM = ("---\ntype: {t}\ntopic: 某主题\ndate: 2026-09-19\nauthor: {a}\n"
      "status: draft\ncarrier: session-local\n---\n\n")

GOOD_RESEARCH = FM.format(t="Research", a="Researcher") + """# 某主题调研

## 一、执行摘要
结论先行。

## 二、多方案对比矩阵
| 维度 | 方案A | 方案B |
|---|---|---|
| 证据等级 | A | B |
| 时效性 | 高 | 中 |
| 风险 | 低 | 高 |

## 三、主推方案与落地路径
步骤一。信源 https://example.org/spec 与 https://example.org/paper

## 四、风险与 Plan B
风险一。
"""


class ResearchStructureTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _check(self, text: str, name: str = "01_调研.md") -> list[str]:
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return vs.check_markdown(p)

    def test_compliant_research_passes(self) -> None:
        self.assertEqual(self._check(GOOD_RESEARCH), [])

    def test_missing_urls_blocked(self) -> None:
        """无链接的引用无法复核，等同于未取证——这是「强制联网」可机械验证的必要条件。"""
        errs = self._check(GOOD_RESEARCH.replace("https://example.org/spec 与 https://example.org/paper", "见官方文档"))
        self.assertTrue(any("信源" in e for e in errs), errs)

    def test_single_option_matrix_blocked(self) -> None:
        """只有一个候选的"对比矩阵"是自卖自夸，不是横向对比。"""
        thin = GOOD_RESEARCH.replace("| 时效性 | 高 | 中 |\n| 风险 | 低 | 高 |\n", "")
        self.assertTrue(any("对比表" in e for e in self._check(thin)))

    def test_missing_section_blocked(self) -> None:
        self.assertTrue(any("缺少章节" in e for e in self._check(GOOD_RESEARCH.replace("## 四、风险与 Plan B", "## 四、其他"))))

    def test_design_uses_stage_rules_not_type(self) -> None:
        """03_设计 与 02_方案 同为 type: Proposal 却结构迥异，只按 type 取判据必然误判一方。"""
        design = FM.format(t="Proposal", a="Designer") + (
            "# 设计\n\n## 一、页面拓扑\n\n## 二、字段动静映射\n\n## 三、交互事件与状态\n\n## 四、原型索引\n[demo](prototypes/demo.html)\n")
        self.assertEqual(self._check(design, "03_设计.md"), [])
        # 同一份内容按 02_方案 判则缺边界与决策对比
        self.assertTrue(self._check(design, "02_方案.md"))

    def test_labels_in_tables_and_bold_count(self) -> None:
        """本工作区的报告惯用表头列与加粗承载章节角色，只扫标题会把合规报告判错。"""
        report = FM.format(t="Report", a="Maintainer") + (
            "# 验收\n\n| 检查对象 | 证据 | 结论 |\n|---|---|---|\n| 测试 | 42 passed | 通过 |\n\n**未覆盖范围**：无。\n")
        self.assertEqual(self._check(report, "07_验收报告.md"), [])

    def test_unknown_type_is_not_gated(self) -> None:
        """没有判据的类型不拦（空态即初始态），但也不假装检查过。"""
        spec = FM.format(t="Spec", a="Builder").replace("status: draft", "id: Tech-1\nstatus: draft") + "# Spec\n"
        self.assertEqual(vs.check_deliverable_structure(self.dir / "x.md", {"type": "Spec"}), [])
        self.assertIsNotNone(spec)
