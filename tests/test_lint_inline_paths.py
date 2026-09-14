"""行内代码路径引用的断链核验。

规则正文引用工具与实例声明时大量使用反引号内联形态（如
`python3 .entropaxis/tools/init_capsule.py`），它不是 Markdown 链接。只扫 `](...)` 会让
这类引用成为门禁盲区——真实事故：`工作流指令.md` 引用了一个未纳入版本库的工具，
本机全绿、分发后收件方拿到一条指向不存在文件的指令。
"""

import tempfile
import unittest
from pathlib import Path

from tools import paths
from tools.lint_workspace import check_data_declaration_links, check_system_markdown_links


def _mk_workspace(root: Path, rule_body: str) -> None:
    rules = root / paths.SYSTEM_DIRNAME / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (root / paths.SYSTEM_DIRNAME / "data" / "rules").mkdir(parents=True, exist_ok=True)
    (rules / "demo.md").write_text(rule_body, encoding="utf-8")


class InlineCodePathLintTests(unittest.TestCase):
    def test_missing_system_tool_is_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_workspace(root, "执行 `python3 .entropaxis/tools/ghost.py <父目录>` 生成骨架。")
            issues = check_system_markdown_links(root)
            self.assertTrue(any("ghost.py" in i for i in issues), issues)

    def test_existing_system_tool_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_workspace(root, "执行 `python3 .entropaxis/tools/real.py` 生成骨架。")
            (root / paths.SYSTEM_DIRNAME / "tools").mkdir(parents=True, exist_ok=True)
            (root / paths.SYSTEM_DIRNAME / "tools" / "real.py").write_text("", encoding="utf-8")
            self.assertEqual(check_system_markdown_links(root), [])

    def test_missing_data_declaration_is_advisory_not_blocking(self) -> None:
        """`data/` 引用缺失是合法初始态，只进建议项；进阻断项会让分发后开箱即红。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_workspace(root, "参数见 `.entropaxis/data/rules/ghost-config.md`。")
            self.assertEqual(check_system_markdown_links(root), [])
            advisory = check_data_declaration_links(root)
            self.assertTrue(any("ghost-config.md" in i for i in advisory), advisory)

    def test_placeholder_and_glob_forms_are_ignored(self) -> None:
        """示例占位与通配模板不是真实引用，命中即噪声。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_workspace(
                root,
                "新建 `.entropaxis/tools/<name>.py`；映射 `.entropaxis/data/templates/X` ⟺ "
                "`.entropaxis/templates/X.template.*`；凭证放 `.entropaxis/data/credentials/<skill-name>/`。",
            )
            self.assertEqual(check_system_markdown_links(root), [])
            self.assertEqual(check_data_declaration_links(root), [])

    def test_plain_prose_path_outside_backticks_is_ignored(self) -> None:
        """只认行内代码：散文里提到目录名不构成引用契约。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_workspace(root, "系统研究产物归位 .entropaxis/tools/ghost.py 所在层级。")
            self.assertEqual(check_system_markdown_links(root), [])


if __name__ == "__main__":
    unittest.main()
