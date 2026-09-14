import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from tools.bootstrap import sync_entrypoints, detect_host_apps, init_file_opener

# ponytail: sync_entrypoints() 的路径由自身 __file__ 派生，无法用 monkeypatch 隔离到临时目录；
# 直接对真实工作区跑，并用 finally 恢复，覆盖幂等性与漂移自愈两条核心路径。


class BootstrapSyncTests(TestCase):
    def test_sync_is_idempotent(self) -> None:
        self.assertTrue(sync_entrypoints(verbose=False))
        self.assertTrue(sync_entrypoints(verbose=False))
        for filename in ("AGENTS.md", "CLAUDE.md"):
            source = SYSTEM_ROOT / "entrypoints" / filename
            target = SYSTEM_ROOT.parent / filename
            self.assertEqual(target.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))

    def test_sync_self_heals_drifted_target(self) -> None:
        target = SYSTEM_ROOT.parent / "AGENTS.md"
        source = SYSTEM_ROOT / "entrypoints" / "AGENTS.md"
        original = target.read_text(encoding="utf-8")
        try:
            target.write_text("drifted content", encoding="utf-8")
            self.assertTrue(sync_entrypoints(verbose=False))
            self.assertEqual(target.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
        finally:
            target.write_text(original, encoding="utf-8")



class FileOpenerMergeTests(TestCase):
    """merge-preserve 策略：人工仲裁项持久保留；全量重扫仅显式触发（G-13）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="file-opener-"))
        self.system_dir = self.tmp / ".entropaxis"
        (self.system_dir / "templates" / "instance").mkdir(parents=True)
        shutil.copy(
            SYSTEM_ROOT / "templates" / "instance" / "file-opener.template.json",
            self.system_dir / "templates" / "instance" / "file-opener.template.json",
        )
        self.data_dir = self.system_dir / "data"
        (self.data_dir / "templates").mkdir(parents=True)
        # 模板渲染产物落 data/templates/，与源模板同名，路径即来源指针
        self.config_path = self.data_dir / "templates" / "file-opener.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _arbitrated_config(self) -> dict:
        return {
            "version": "1.0.0",
            "platform": "darwin",
            "associations": {
                "markdown": {
                    "name": "Markdown与标记文档",
                    "extensions": [".md"],
                    "selected_app": "Orca (orca file open)",
                    "command": "orca file open",
                    "available_candidates": ["Orca (orca file open)"],
                }
            },
        }

    def test_default_run_preserves_arbitration_and_fills_missing(self) -> None:
        self.config_path.write_text(json.dumps(self._arbitrated_config(), ensure_ascii=False), encoding="utf-8")
        cfg = init_file_opener(verbose=False, system_dir=self.system_dir)
        self.assertEqual(cfg["associations"]["markdown"]["command"], "orca file open")
        self.assertIn("word", cfg["associations"])
        on_disk = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["associations"]["markdown"]["command"], "orca file open")
        self.assertIn("word", on_disk["associations"])

    def test_force_rescan_rebuilds_from_template(self) -> None:
        self.config_path.write_text(json.dumps(self._arbitrated_config(), ensure_ascii=False), encoding="utf-8")
        cfg = init_file_opener(verbose=False, force_rescan=True, system_dir=self.system_dir)
        self.assertNotEqual(cfg["associations"]["markdown"]["command"], "orca file open")

    def test_corrupt_config_self_heals(self) -> None:
        self.config_path.write_text("{not-json", encoding="utf-8")
        cfg = init_file_opener(verbose=False, system_dir=self.system_dir)
        self.assertIn("markdown", cfg["associations"])
        json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_rerun_without_change_keeps_file_stable(self) -> None:
        self.config_path.write_text(json.dumps(self._arbitrated_config(), ensure_ascii=False), encoding="utf-8")
        first = init_file_opener(verbose=False, system_dir=self.system_dir)
        stamp = self.config_path.stat().st_mtime_ns
        second = init_file_opener(verbose=False, system_dir=self.system_dir)
        self.assertEqual(first, second)
        self.assertEqual(self.config_path.stat().st_mtime_ns, stamp)
