#!/usr/bin/env python3
"""
Unit tests for .entropaxis/tools/scaffold_demo.py
"""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.scaffold_demo import generate_demo_html, lint_demo_html


class TestScaffoldDemo(unittest.TestCase):
    def test_generate_demo_html_structure(self):
        title = "测试交互原型系统"
        stages = ["阶段A", "阶段B", "阶段C"]
        html = generate_demo_html(title, stages)

        self.assertIn("<title>测试交互原型系统</title>", html)
        self.assertIn("阶段A", html)
        self.assertIn("阶段B", html)
        self.assertIn("阶段C", html)
        # Check IPO 3 columns
        self.assertIn("INPUT", html)
        self.assertIn("PROCESS", html)
        self.assertIn("OUTPUT", html)
        # Check Output width expanded to 630px
        self.assertIn("--col-output-w: 630px;", html)
        # Check warm query card with human wording
        self.assertIn("录入报错提示语（数据录错时，弹给业务一线看的话）", html)
        # Check empty state & mock dropzone
        self.assertIn("empty-state", html)
        self.assertIn("file-dropzone", html)

    def test_lint_on_generated_demo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "demo.html"
            html = generate_demo_html("有效Demo", ["输入解析", "逻辑配置", "导出交付"])
            out_path.write_text(html, encoding="utf-8")

            passed, issues = lint_demo_html(out_path)
            self.assertTrue(passed, f"生成的标准 Demo 应该通过体检，实际问题: {issues}")
            self.assertEqual(len(issues), 0)

    def test_lint_detects_broken_html(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_path = Path(tmpdir) / "bad.html"
            bad_content = """<html><body>
            <h1>无规范页面</h1>
            <div class="terminal">$ medaikaid-agent parse ...</div>
            <div style="grid-template-columns: 200px 300px 200px;">拥挤内容</div>
            </body></html>"""
            bad_path.write_text(bad_content, encoding="utf-8")

            passed, issues = lint_demo_html(bad_path)
            self.assertFalse(passed)
            self.assertTrue(any("步进器" in i for i in issues))
            self.assertTrue(any("三栏" in i for i in issues))
            self.assertTrue(any("终端" in i for i in issues))
            self.assertTrue(any("报错提示语" in i for i in issues))
            self.assertTrue(any("空状态" in i for i in issues))

    def test_cli_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "cli_demo.html"
            # 1. Generate
            cmd_gen = [
                sys.executable,
                str(Path(__file__).resolve().parent.parent / "tools" / "scaffold_demo.py"),
                "--title", "CLI测试Demo",
                "--stages", "步骤1,步骤2,步骤3",
                "--out", str(out_file)
            ]
            res_gen = subprocess.run(cmd_gen, capture_output=True, text=True)
            self.assertEqual(res_gen.returncode, 0, res_gen.stderr)
            self.assertTrue(out_file.exists())
            self.assertIn("已成功原子生成交互 Demo 脚手架", res_gen.stdout)

            # 2. Lint
            cmd_lint = [
                sys.executable,
                str(Path(__file__).resolve().parent.parent / "tools" / "scaffold_demo.py"),
                "--lint", str(out_file)
            ]
            res_lint = subprocess.run(cmd_lint, capture_output=True, text=True)
            self.assertEqual(res_lint.returncode, 0, res_lint.stderr)
            self.assertIn("Demo 规范体检全绿通过", res_lint.stdout)


if __name__ == "__main__":
    unittest.main()
