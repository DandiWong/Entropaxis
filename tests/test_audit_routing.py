import json
import sys
from pathlib import Path
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

import audit_routing as AR  # noqa: E402
import hook_route_match as HRM  # noqa: E402


class NormalizeTests(TestCase):
    def test_strips_whitespace_and_case(self) -> None:
        self.assertEqual(AR.normalize("纳入 Entropaxis"), "纳入entropaxis")

    def test_audit_and_hook_normalize_identically(self) -> None:
        """两处独立实现必须同口径，否则审计结果不代表 hook 的真实行为。"""
        for sample in ("纳入 Entropaxis", "  TIP  ", "新 Feature", "同步任务"):
            self.assertEqual(AR.normalize(sample), HRM.normalize(sample))


class MatchTests(TestCase):
    def setUp(self) -> None:
        self.routes = [
            {"mechanism": "审计", "keywords": ["审计"], "exclude": ["根据审计"]},
            {"mechanism": "修正", "keywords": ["根据审计修正"]},
            {"mechanism": "tip", "keywords": ["tip"], "match_type": "exact"},
        ]

    def test_exclude_suppresses_substring_false_trigger(self) -> None:
        names = [r["mechanism"] for r in AR.match_prompt("根据审计修正", self.routes)]
        self.assertNotIn("审计", names)
        self.assertIn("修正", names)

    def test_plain_keyword_still_hits(self) -> None:
        names = [r["mechanism"] for r in AR.match_prompt("对方案做一次审计", self.routes)]
        self.assertEqual(names, ["审计"])

    def test_exact_match_rejects_embedded_word(self) -> None:
        self.assertEqual(AR.match_prompt("给我一些 tips 参考", self.routes), [])
        self.assertEqual(len(AR.match_prompt(" TIP ", self.routes)), 1)

    def test_audit_and_hook_match_identically(self) -> None:
        for prompt in ("根据审计修正", "对方案做一次审计", " TIP ", "无关问题"):
            self.assertEqual(
                [r["mechanism"] for r in AR.match_prompt(prompt, self.routes)],
                [r["mechanism"] for r in HRM.match_prompt(prompt, self.routes)],
            )


class ExtractSectionTests(TestCase):
    DOC = "# 标题\n前言\n\n## 甲\n甲正文\n\n### 甲一\n细节\n\n## 乙\n乙正文\n"

    def test_extracts_until_same_level_heading(self) -> None:
        got = AR.extract_section(self.DOC, "## 甲")
        self.assertIn("甲正文", got)
        self.assertIn("细节", got)  # 更深层级属于本节
        self.assertNotIn("乙正文", got)

    def test_last_section_runs_to_end(self) -> None:
        self.assertIn("乙正文", AR.extract_section(self.DOC, "## 乙"))

    def test_missing_anchor_returns_empty(self) -> None:
        self.assertEqual(AR.extract_section(self.DOC, "## 不存在"), "")


class CostTests(TestCase):
    def test_estimate_tokens_counts_cjk_per_char(self) -> None:
        self.assertEqual(AR.estimate_tokens("中文四字"), 4)
        self.assertGreater(AR.estimate_tokens("abcdefgh"), 0)

    def test_anchor_cost_never_exceeds_full_file(self) -> None:
        cost = AR.audit_cost(AR.load_routes())
        for sc in cost["scenarios"]:
            self.assertLessEqual(sc["total_anchor_only"], sc["total"], sc["mechanism"])


class CorpusTests(TestCase):
    """测试集本身的回归保护：路由表与用例集必须保持同步。"""

    def setUp(self) -> None:
        self.routes = AR.load_routes()
        self.cases = AR.load_cases()
        self.coverage = AR.audit_coverage(self.routes, self.cases)

    def test_no_missed_or_false_triggers(self) -> None:
        failures = [
            r for r in self.coverage["results"]
            if r["kind"] != "paraphrase" and (r["missed"] or r["extra"])
        ]
        self.assertEqual(failures, [], f"路由漏检或误触发：{json.dumps(failures, ensure_ascii=False)}")

    def test_every_mechanism_has_a_case(self) -> None:
        self.assertEqual(
            self.coverage["untested_mechanisms"], [],
            "route_map.json 中存在未被任何用例覆盖的机制，等于无回归保护。",
        )

    def test_routed_files_exist(self) -> None:
        workspace = SYSTEM_ROOT.parent
        for route in self.routes:
            for rel in route.get("files", []):
                self.assertTrue((workspace / rel).exists(), f"{route['mechanism']} 指向的 {rel} 不存在")


if __name__ == "__main__":
    import unittest

    unittest.main()
