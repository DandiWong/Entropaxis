import json
import tempfile
import unittest
from pathlib import Path

from tools import paths
from tools.lint_workspace import check_board_config_no_credentials


def _write_board_config(root: Path, providers: dict) -> None:
    d = root / paths.SYSTEM_DIRNAME / "data" / "templates"
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

    def test_credential_in_non_cli_field_is_flagged(self) -> None:
        """绕过面 1：把 Token 换个字段名（非 cli 数组）不能逃过检查。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_board_config(root, {"main": {"cli": ["dash.py"], "token": "abc123-token=xyz"}})
            issues = check_board_config_no_credentials(root)
            self.assertEqual(len(issues), 1)

    def test_credential_embedded_in_shell_string_is_flagged(self) -> None:
        """绕过面 2：整段拼进 shell -c 字符串也要能命中子串匹配。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_board_config(root, {"main": {"cli": ["sh", "-c", "dash.py --token secret"]}})
            issues = check_board_config_no_credentials(root)
            self.assertEqual(len(issues), 1)

    def test_bare_short_flag_is_flagged(self) -> None:
        """绕过面 3：无长选项前缀、用 -t 空格值的裸凭证参数。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_board_config(root, {"main": {"cli": ["dash.py", "-t secret"]}})
            issues = check_board_config_no_credentials(root)
            self.assertEqual(len(issues), 1)

    def test_credential_field_name_flagged_even_without_marker_in_value(self) -> None:
        """第 5 轮实测抓到的绕过：值本身不含任何标志性子串，但字段名叫 token。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_board_config(root, {"main": {"cli": ["dash.py"], "token": "abc123"}})
            issues = check_board_config_no_credentials(root)
            self.assertEqual(len(issues), 1)
            self.assertIn("字段名", issues[0])

    def test_token_field_with_nested_object_value_still_flagged(self) -> None:
        """第 6 轮实测抓到：{"token": {"value": "abc"}} 递归时父字段名 token 曾被子键
        value 覆盖丢失，两层都查不出凭证特征。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_board_config(root, {"main": {"cli": ["dash.py"], "token": {"value": "abc123"}}})
            issues = check_board_config_no_credentials(root)
            self.assertEqual(len(issues), 1)
            self.assertIn("token", issues[0])

    def test_token_field_with_non_string_value_still_flagged(self) -> None:
        """{"token": 123456}：值不是字符串，此前完全不产出叶子、不被检查。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_board_config(root, {"main": {"cli": ["dash.py"], "token": 123456}})
            issues = check_board_config_no_credentials(root)
            self.assertEqual(len(issues), 1)

    def test_headers_with_custom_auth_key_not_over_flagged(self) -> None:
        """headers 本身命中即可报一次；子键（如 X-Auth）不需要单独再命中，
        不应产生重复告警。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_board_config(root, {"main": {"cli": ["dash.py"], "headers": {"X-Auth": "abc123"}}})
            issues = check_board_config_no_credentials(root)
            self.assertEqual(len(issues), 1)

    def test_nested_headers_authorization_field_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_board_config(
                root, {"main": {"cli": ["dash.py"], "headers": {"Authorization": "Bearer abc123"}}}
            )
            issues = check_board_config_no_credentials(root)
            self.assertGreaterEqual(len(issues), 1)

    def test_malformed_json_fails_closed_not_silently(self) -> None:
        """畸形 JSON 必须报违规，不能因为"解析不了"就悄悄放行。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / paths.SYSTEM_DIRNAME / "data" / "templates"
            d.mkdir(parents=True)
            (d / "board_config.json").write_text("{not valid json", encoding="utf-8")
            issues = check_board_config_no_credentials(root)
            self.assertEqual(len(issues), 1)
            self.assertIn("无法解析", issues[0])


if __name__ == "__main__":
    unittest.main()
