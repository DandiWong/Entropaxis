import sys
from pathlib import Path
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from tools.bootstrap import sync_root_configs, detect_host_apps, init_file_opener, get_open_command

# ponytail: sync_root_configs() 的路径由自身 __file__ 派生，无法用 monkeypatch 隔离到临时目录；
# 直接对真实工作区跑，并用 finally 恢复，覆盖幂等性与漂移自愈两条核心路径。


class BootstrapSyncTests(TestCase):
    def test_sync_is_idempotent(self) -> None:
        self.assertTrue(sync_root_configs(verbose=False))
        self.assertTrue(sync_root_configs(verbose=False))
        for filename in ("AGENTS.md", "CLAUDE.md"):
            source = SYSTEM_ROOT / "root-configs" / filename
            target = SYSTEM_ROOT.parent / filename
            self.assertEqual(target.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))

    def test_sync_self_heals_drifted_target(self) -> None:
        target = SYSTEM_ROOT.parent / "AGENTS.md"
        source = SYSTEM_ROOT / "root-configs" / "AGENTS.md"
        original = target.read_text(encoding="utf-8")
        try:
            target.write_text("drifted content", encoding="utf-8")
            self.assertTrue(sync_root_configs(verbose=False))
            self.assertEqual(target.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
        finally:
            target.write_text(original, encoding="utf-8")

    def test_file_opener_initialization_and_command(self) -> None:
        # Test file opener initialization
        cfg = init_file_opener(verbose=False, force_rescan=True)
        self.assertIsInstance(cfg, dict)
        self.assertIn("associations", cfg)
        self.assertIn("word", cfg["associations"])
        self.assertIn("markdown", cfg["associations"])

        # Test get_open_command with various formats
        cmd_docx = get_open_command("01_test/doc.docx", SYSTEM_ROOT.parent)
        self.assertIn("doc.docx", cmd_docx)
        cmd_pptx = get_open_command("01_test/slide.pptx", SYSTEM_ROOT.parent)
        self.assertIn("slide.pptx", cmd_pptx)
        cmd_dir = get_open_command("01_test/dir/", SYSTEM_ROOT.parent)
        self.assertIn("01_test/dir/", cmd_dir)
