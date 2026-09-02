import json
import tempfile
import unittest
from pathlib import Path

from tools.sync_board import (
    STATUS_MAP,
    _provider_cli,
    load_board,
    load_providers,
    norm_status,
)

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
import tools.sync_board as sync_board


class SyncBoardTests(unittest.TestCase):
    def test_status_map_and_norm(self) -> None:
        self.assertEqual(STATUS_MAP["done"], ("done", "done"))
        self.assertEqual(norm_status("🔧"), "active")
        self.assertEqual(norm_status("✅"), "done")
        with self.assertRaises(SystemExit):
            norm_status("bogus")

    def test_load_board_nested_and_flat_compat(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            docs = Path(td) / "docs"
            docs.mkdir()
            (docs / ".board.json").write_text(
                json.dumps({"ns": "N", "boards": {"main": "m1", "dev": "d1"}}),
                encoding="utf-8",
            )
            bd = load_board(td)
        self.assertEqual(bd["main_id"], "m1")
        self.assertEqual(bd["dev_id"], "d1")
        self.assertEqual(bd["ns"], "N")

    def test_load_board_flat_abstract_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            docs = Path(td) / "docs"
            docs.mkdir()
            (docs / ".board.json").write_text(
                json.dumps({"ns": "N", "main_project": "m2", "dev_project": "d2"}),
                encoding="utf-8",
            )
            bd = load_board(td)
        self.assertEqual(bd["main_id"], "m2")
        self.assertEqual(bd["dev_id"], "d2")

    def test_providers_declared_via_data_file(self) -> None:
        original = sync_board.PROVIDERS_FILE
        try:
            with tempfile.TemporaryDirectory() as td:
                f = Path(td) / "board_config.json"
                f.write_text(
                    json.dumps({"providers": {"main": {"cli": ["python3", "x.py"]}, "dev": {"cli": ["devcli"]}}}),
                    encoding="utf-8",
                )
                sync_board.PROVIDERS_FILE = f
                self.assertEqual(_provider_cli("main"), ["python3", "x.py"])
                self.assertEqual(_provider_cli("dev"), ["devcli"])
                self.assertIsNone(_provider_cli("ext"))
                # 声明缺失 → 空 Provider，全降级
                sync_board.PROVIDERS_FILE = Path(td) / "missing.json"
                self.assertEqual(load_providers(), {})
                self.assertIsNone(_provider_cli("main"))
        finally:
            sync_board.PROVIDERS_FILE = original


if __name__ == "__main__":
    unittest.main()
