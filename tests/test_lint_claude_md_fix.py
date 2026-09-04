import tempfile
import unittest
from pathlib import Path

from tools.lint_workspace import check_claude_md_thin_shell, fix_claude_md_thin_shell


class ClaudeMdAutoFixTests(unittest.TestCase):
    def test_fix_rewrites_violation_to_standard_thin_shell(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = root / "demo-project"
            proj.mkdir()
            (proj / "AGENTS.md").write_text("# demo\n", encoding="utf-8")
            (proj / "CLAUDE.md").write_text(
                "# demo\n\n多余正文，不是纯薄壳。\n@AGENTS.md\n", encoding="utf-8"
            )

            self.assertTrue(check_claude_md_thin_shell(root))
            fixed = fix_claude_md_thin_shell(root)
            self.assertEqual(fixed, ["demo-project/CLAUDE.md"])
            self.assertEqual(
                (proj / "CLAUDE.md").read_text(encoding="utf-8"),
                "# demo\n@AGENTS.md\n",
            )
            self.assertEqual(check_claude_md_thin_shell(root), [])

    def test_fix_generates_default_title_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = root / "no-title-project"
            proj.mkdir()
            (proj / "CLAUDE.md").write_text("just some text\n", encoding="utf-8")

            fixed = fix_claude_md_thin_shell(root)
            self.assertEqual(fixed, ["no-title-project/CLAUDE.md"])
            content = (proj / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertEqual(
                content, "# no-title-project · Claude Code 入口\n@AGENTS.md\n"
            )

    def test_fix_and_check_skip_nested_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            nested = root / "09知识产权" / "PatentWriterAgent"
            nested.mkdir(parents=True)
            (nested / ".git").mkdir()
            (nested / "CLAUDE.md").write_text(
                "# 专利写作项目管理指令\n\n这是一份有实质内容的上游项目 CLAUDE.md。\n",
                encoding="utf-8",
            )

            self.assertEqual(check_claude_md_thin_shell(root), [])
            self.assertEqual(fix_claude_md_thin_shell(root), [])
            self.assertIn("专利写作项目管理指令", (nested / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_fix_skips_already_compliant_and_excluded_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ok_proj = root / "ok-project"
            ok_proj.mkdir()
            (ok_proj / "CLAUDE.md").write_text("# ok\n@AGENTS.md\n", encoding="utf-8")

            archive = root / "Archive" / "old-project"
            archive.mkdir(parents=True)
            (archive / "CLAUDE.md").write_text("legacy content\n", encoding="utf-8")

            fixed = fix_claude_md_thin_shell(root)
            self.assertEqual(fixed, [])
            self.assertEqual(
                (archive / "CLAUDE.md").read_text(encoding="utf-8"), "legacy content\n"
            )


if __name__ == "__main__":
    unittest.main()
