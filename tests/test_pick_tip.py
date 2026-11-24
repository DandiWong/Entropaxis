import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.pick_tip import format_tip, pick_tip


class PickTipTests(unittest.TestCase):
    def test_only_tip_entries_are_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "tips.md"
            src.write_text(
                "# 工作空间 Tips\n> 说明段落不参与展示\n"
                "- TIP：全角冒号条目\n- TIP:半角冒号条目\n"
                "- 普通条目不展示\n正文不展示\n",
                encoding="utf-8",
            )
            drawn = {pick_tip(src) for _ in range(60)}
            self.assertEqual(drawn, {"- TIP：全角冒号条目", "- TIP:半角冒号条目"})

    def test_missing_source_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(pick_tip(Path(td) / "不存在的 tips.md"))

    def test_unreadable_source_is_silent(self) -> None:
        """真源是目录（或不可读）时静默跳过，不抛栈——契约要求会话首条回复照常输出。"""
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(pick_tip(Path(td)))

    def test_source_without_tip_entries_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "tips.md"
            src.write_text("# 工作空间 Tips\n> 尚未登记任何条目\n", encoding="utf-8")
            self.assertIsNone(pick_tip(src))

    def test_format_tip_block(self) -> None:
        raw_full = "- TIP：这是全角冒号提示"
        formatted_full = format_tip(raw_full)
        self.assertEqual(formatted_full, "---\n> 💡 **Tip**：这是全角冒号提示")

        raw_half = "- TIP:这是半角冒号提示"
        formatted_half = format_tip(raw_half)
        self.assertEqual(formatted_half, "---\n> 💡 **Tip**：这是半角冒号提示")

    def test_cli_outputs_formatted_by_default_and_raw_with_flag(self) -> None:
        script = Path(__file__).resolve().parent.parent / "tools" / "pick_tip.py"
        proc_default = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            check=True,
        )
        if proc_default.stdout.strip():
            self.assertTrue(proc_default.stdout.startswith("---\n> 💡 **Tip**："))

        proc_raw = subprocess.run(
            [sys.executable, str(script), "--raw"],
            capture_output=True,
            text=True,
            check=True,
        )
        if proc_raw.stdout.strip():
            self.assertTrue(
                proc_raw.stdout.startswith("- TIP：") or proc_raw.stdout.startswith("- TIP:")
            )


if __name__ == "__main__":
    unittest.main()
