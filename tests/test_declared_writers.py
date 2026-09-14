"""声明写入者存在性契约（《01_根系统治理》SOP 第 2 步「写入者必须真实存在」）。

真实事故：`registry.md` 正文与来源标记都声明「唯一写入者：init-project Skill」，
而该 Skill 与其工具都从不写它——注册表在每台新机器上永远是空表，依赖它的项目归属、
第 7 项反向校验与外部看板映射全部静默失效。声明了写入者比不声明更危险，因为读者
会以为它已经被管起来了。
"""

import tempfile
import textwrap
import unittest
from pathlib import Path

from tools import paths
from tools.lint_workspace import check_declared_writers


def _mk_control_plane(root: Path, known: str, *, tools: dict | None = None, skills: dict | None = None) -> None:
    system = root / paths.SYSTEM_DIRNAME
    (system / "tools").mkdir(parents=True, exist_ok=True)
    (system / "tools" / "stamp_data_provenance.py").write_text(
        textwrap.dedent(f"""\
        KNOWN = {known}
        """),
        encoding="utf-8",
    )
    for name, body in (tools or {}).items():
        (system / "tools" / name).write_text(body, encoding="utf-8")
    for name, body in (skills or {}).items():
        d = system / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")


class DeclaredWriterTests(unittest.TestCase):
    def test_skill_declared_as_writer_that_never_writes_is_caught(self) -> None:
        """本轮真实缺陷的最小复现：Skill 存在，但它的 SKILL.md 根本没提这个文件。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_control_plane(
                root,
                '{"registry.md": {"source": "人工创建", "managed_by": "init-project Skill"}}',
                skills={"init-project": "# 初始化项目\n创建目录结构。\n"},
            )
            issues = check_declared_writers(root)
            self.assertEqual(len(issues), 1, issues)
            self.assertIn("名不副实", issues[0])
            self.assertIn("registry.md", issues[0])

    def test_skill_that_actually_writes_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_control_plane(
                root,
                '{"registry.md": {"source": "人工创建", "managed_by": "init-project Skill"}}',
                skills={"init-project": "# 初始化项目\n立项时追加一行到 registry.md。\n"},
            )
            self.assertEqual(check_declared_writers(root), [])

    def test_missing_writer_tool_is_caught(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_control_plane(root, '{"x.json": {"source": "人工创建", "managed_by": "ghost.py"}}')
            issues = check_declared_writers(root)
            self.assertTrue(any("写入者不存在" in i for i in issues), issues)

    def test_stale_symbol_in_declaration_is_caught(self) -> None:
        """工具改了函数名而声明没跟——声明指向一个已不存在的入口。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_control_plane(
                root,
                '{"x.json": {"source": "人工创建", "managed_by": "boot.py render_old_name"}}',
                tools={"boot.py": "def render_new_name():\n    pass\n"},
            )
            issues = check_declared_writers(root)
            self.assertTrue(any("名不副实" in i and "render_old_name" in i for i in issues), issues)

    def test_stale_template_source_path_is_caught(self) -> None:
        """模板搬家后来源声明最易失修：本轮实测一次抓到三条。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_control_plane(
                root,
                '{"x.json": {"source": ".entropaxis/templates/x.template.json", "managed_by": "人工"}}',
            )
            issues = check_declared_writers(root)
            self.assertTrue(any("来源声明失效" in i for i in issues), issues)

    def test_human_maintained_declarations_are_not_mechanically_checked(self) -> None:
        """人是否落笔无法静态判定，只声明人工/Agent 维护的不做核验，避免制造假阳性。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_control_plane(
                root,
                '{"tips.md": {"source": "人工创建", "managed_by": "Agent 按《工作流指令》生成（须用户确认）"}}',
            )
            self.assertEqual(check_declared_writers(root), [])

    def test_absent_provenance_table_degrades_silently(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(check_declared_writers(Path(td)), [])


if __name__ == "__main__":
    unittest.main()
