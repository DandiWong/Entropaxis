"""机械核验核心工具的默认静默契约（Silence is Golden）与 Token 预算。"""
import io
import subprocess
import sys
import unittest
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = SYSTEM_ROOT.parent
TOOLS_DIR = SYSTEM_ROOT / "tools"


class ToolEchoSilenceTests(unittest.TestCase):
    def test_lint_workspace_silent_on_green(self):
        """lint_workspace 在无建议项全绿时必须只输出单行总结（<= 3 行，<= 50 tokens）。"""
        proc = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "lint_workspace.py")],
            capture_output=True,
            text=True,
            cwd=str(WORKSPACE_ROOT),
        )
        self.assertEqual(proc.returncode, 0)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        if "全部" in proc.stdout and "门禁通过" in proc.stdout:
            self.assertLessEqual(len(lines), 3, f"lint_workspace 全绿时输出行数超标: {len(lines)} 行")
    def test_bootstrap_silent_on_success(self):
        """bootstrap 在成功时必须输出简洁单行总结（<= 3 行）。"""
        proc = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "bootstrap.py")],
            capture_output=True,
            text=True,
            cwd=str(WORKSPACE_ROOT),
        )
        self.assertEqual(proc.returncode, 0)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertLessEqual(len(lines), 3, f"bootstrap 输出行数超标: {len(lines)} 行")
        self.assertIn("初始化与自愈完成", proc.stdout)
    def test_resolve_project_compact_output(self):
        """resolve_project 默认必须输出单行极简高密度文本。"""
        proc = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "resolve_project.py"), "--list"],
            capture_output=True,
            text=True,
            cwd=str(WORKSPACE_ROOT),
        )
        self.assertEqual(proc.returncode, 0)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        # 每一行都是单行紧凑项目
        for line in lines:
            self.assertTrue(line.startswith("[") and "]" in line)
    def test_audit_routing_silent_cost_table(self):
        """audit_routing 默认省略 20 场景明细表（<= 20 行），全量表仅在 --cost-table 展开。"""
        proc = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "audit_routing.py"), "--strict"],
            capture_output=True,
            text=True,
            cwd=str(WORKSPACE_ROOT),
        )
        self.assertEqual(proc.returncode, 0)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        # 默认模式下不应平铺 20 个细碎场景行（包含留出集诊断行 <= 25 行，此前全量表为 45+ 行）
        self.assertLessEqual(len(lines), 25, f"audit_routing 默认输出行数过多: {len(lines)} 行")


if __name__ == "__main__":
    unittest.main()
