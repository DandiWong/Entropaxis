import sys
from pathlib import Path
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from tools.bootstrap import sync_root_configs

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
