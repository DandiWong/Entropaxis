import json
import tempfile
import unittest
from pathlib import Path

from tools.lint_workspace import check_board_config_no_credentials


def _write_board_config(root: Path, providers: dict) -> None:
    d = root / ".data" / "templates"
    d.mkdir(parents=True, exist_ok=True)
    (d / "board_config.json").write_text(
        json.dumps({"providers": providers}), encoding="utf-8"
    )


class BoardConfigCredentialLintTests(unittest.TestCase):
    def test_missing_file_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(check_board_config_no_credentials(Path(td)), [])

    def test_clean_provider_cli_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_board_config(root, {"main": {"cli": ["python3", "dash.py"]}})
            self.assertEqual(check_board_config_no_credentials(root), [])

    def test_token_flag_in_cli_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_board_config(root, {"main": {"cli": ["dash.py", "--token=abc123"]}})
            issues = check_board_config_no_credentials(root)
            self.assertEqual(len(issues), 1)
            self.assertIn("凭证泄漏", issues[0])


if __name__ == "__main__":
    unittest.main()
