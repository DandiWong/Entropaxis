import tempfile
import unittest
from pathlib import Path

from tools import paths
from tools.lint_workspace import SCENARIO_MAX_LINES, check_scenario_cascade_budget


def _mk_control_plane(root: Path) -> Path:
    s = root / paths.SYSTEM_DIRNAME
    for sub in ("rules", "entrypoints", "schemas"):
        (s / sub).mkdir(parents=True, exist_ok=True)
    return s


def _filler(lines: int, body: str = "") -> str:
    return body + "正文\n" * lines


class ScenarioCascadeBudgetTests(unittest.TestCase):
    def test_thin_cascade_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = _mk_control_plane(root)
            (s / "entrypoints" / "AGENTS.md").write_text(
                "按需路由：`.entropaxis/rules/入口.md`\n", encoding="utf-8"
            )
            (s / "rules" / "入口.md").write_text(
                _filler(20, "细则见 [`下游.md`](下游.md)。\n"), encoding="utf-8"
            )
            (s / "rules" / "下游.md").write_text(_filler(20), encoding="utf-8")
            self.assertEqual(check_scenario_cascade_budget(root), [])

    def test_fat_cascade_is_flagged(self) -> None:
        """单文件各自达标、靠互相引用堆出的一次性加载量超预算——这正是逐文件检查的盲区。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = _mk_control_plane(root)
            (s / "entrypoints" / "AGENTS.md").write_text(
                "按需路由：`.entropaxis/rules/胖入口.md` 与 `.entropaxis/rules/瘦入口.md`\n",
                encoding="utf-8",
            )
            (s / "rules" / "胖入口.md").write_text(
                _filler(
                    20,
                    "判据见 [`甲.md`](甲.md)，字段契约见 [`契约`](../schemas/契约.json)。\n",
                ),
                encoding="utf-8",
            )
            (s / "rules" / "瘦入口.md").write_text(_filler(20), encoding="utf-8")
            (s / "rules" / "甲.md").write_text(_filler(SCENARIO_MAX_LINES), encoding="utf-8")
            (s / "schemas" / "契约.json").write_text("{}\n", encoding="utf-8")

            issues = check_scenario_cascade_budget(root)
            self.assertEqual(len(issues), 1, issues)
            self.assertIn("胖入口.md", issues[0])
            self.assertIn("甲.md", issues[0])
            self.assertIn("契约.json", issues[0])
            self.assertNotIn("瘦入口.md", issues[0])

    def test_missing_rules_dir_degrades_quietly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(check_scenario_cascade_budget(Path(td)), [])


if __name__ == "__main__":
    unittest.main()
