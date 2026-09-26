"""体检第 26 项（Skill 版本与 CHANGELOG 一致性）与第 27 项（工具路由存在性）。"""
import sys
import tempfile
import unittest
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
import lint_workspace as lw  # noqa: E402


class SkillMetadataTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.skill = self.root / ".entropaxis" / "skills" / "demo"
        self.skill.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, desc_v="1.1.0", meta_v="1.1.0", changelog_v="1.1.0"):
        (self.skill / "SKILL.md").write_text(
            f'---\nname: demo\ndescription: "v{desc_v}. Demo."\nmetadata:\n  version: "{meta_v}"\n---\n# Demo\n',
            encoding="utf-8")
        if changelog_v:
            (self.skill / "CHANGELOG.md").write_text(f"# Changelog\n\n## [{changelog_v}] - 2026-09-27\n", encoding="utf-8")

    def test_consistent_passes(self):
        self._write()
        self.assertEqual(lw.check_skill_metadata(self.root), [])

    def test_description_mismatch(self):
        self._write(desc_v="1.2.0")
        self.assertTrue(any("description 写 v1.2.0" in i for i in lw.check_skill_metadata(self.root)))

    def test_changelog_behind(self):
        self._write(changelog_v="1.0.0")
        self.assertTrue(any("CHANGELOG 最新版本为 1.0.0" in i for i in lw.check_skill_metadata(self.root)))

    def test_changelog_missing(self):
        self._write(changelog_v=None)
        self.assertTrue(any("CHANGELOG 缺失" in i for i in lw.check_skill_metadata(self.root)))


class ToolRoutingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.system = self.root / ".entropaxis"
        (self.system / "tools").mkdir(parents=True)
        (self.system / "rules").mkdir()
        (self.system / "tools" / "do_thing.py").write_text("print('x')\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_unreferenced_tool_flagged(self):
        issues = lw.check_tool_routing(self.root)
        self.assertTrue(any("do_thing.py" in i for i in issues), issues)

    def test_rule_reference_passes(self):
        (self.system / "rules" / "r.md").write_text("执行 `python3 .entropaxis/tools/do_thing.py`\n", encoding="utf-8")
        self.assertEqual(lw.check_tool_routing(self.root), [])

    def test_self_reference_does_not_count(self):
        (self.system / "tools" / "do_thing.py").write_text("# do_thing 自述\n", encoding="utf-8")
        self.assertTrue(lw.check_tool_routing(self.root))


if __name__ == "__main__":
    unittest.main()
