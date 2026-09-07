import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.sync_board import (
    STATUS_MAP,
    ProviderResult,
    _provider_cli,
    find_issue,
    find_todo_by_source_ref,
    load_board,
    load_providers,
    norm_status,
    upsert_dev,
    upsert_todo,
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

    def test_in_progress_normalizes_to_active_but_external_mapping_unchanged(self) -> None:
        """本地真源规范化：in_progress 输入 → active；外部端映射（STATUS_MAP 输出侧）不变。"""
        self.assertEqual(norm_status("in_progress"), "active")
        self.assertEqual(STATUS_MAP["in_progress"], ("in_progress", "active"))
        self.assertEqual(STATUS_MAP[norm_status("in_progress")], ("in_progress", "active"))

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


def _fake_completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class ProviderFourStateTests(unittest.TestCase):
    """Provider 四态契约：找到不创建、不存在才创建、失败不创建、结果未知先回读。"""

    def setUp(self):
        self._patch = patch.object(sync_board, "_provider_cli", return_value=["fake-cli"])
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_find_issue_found_when_title_matches(self) -> None:
        payload = json.dumps({"issues": [{"id": "abc123", "title": "[M1] 标题"}]})
        with patch("subprocess.run", return_value=_fake_completed(stdout=payload)):
            result = find_issue("proj", "M1", "/tmp")
        self.assertEqual(result.status, "FOUND")
        self.assertEqual(result.id, "abc123")

    def test_find_issue_not_found_when_no_match(self) -> None:
        payload = json.dumps({"issues": []})
        with patch("subprocess.run", return_value=_fake_completed(stdout=payload)):
            result = find_issue("proj", "M1", "/tmp")
        self.assertEqual(result.status, "NOT_FOUND")

    def test_find_issue_failed_on_nonzero_exit(self) -> None:
        with patch("subprocess.run", return_value=_fake_completed(returncode=1, stderr="boom")):
            result = find_issue("proj", "M1", "/tmp")
        self.assertEqual(result.status, "FAILED")

    def test_find_issue_failed_on_invalid_json(self) -> None:
        with patch("subprocess.run", return_value=_fake_completed(stdout="not json")):
            result = find_issue("proj", "M1", "/tmp")
        self.assertEqual(result.status, "FAILED")

    def test_find_issue_unknown_on_timeout(self) -> None:
        """超时须映射为 UNKNOWN（可重试）而非 FAILED——此前两者被一并吞掉，四态形同虚设。"""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=30)):
            result = find_issue("proj", "M1", "/tmp")
        self.assertEqual(result.status, "UNKNOWN")
        self.assertTrue(result.retryable)

    def test_upsert_dev_failed_query_does_not_create(self) -> None:
        with patch("subprocess.run", return_value=_fake_completed(returncode=1, stderr="boom")) as m:
            iid, result = upsert_dev({"dev_id": "proj"}, "/tmp", "M1", "标题", "todo", None)
        self.assertIsNone(iid)
        self.assertEqual(result.status, "FAILED")
        # 只应调用一次（查询失败即返回，不进入创建分支）
        self.assertEqual(m.call_count, 1)

    def test_upsert_dev_not_found_creates_once(self) -> None:
        list_payload = json.dumps({"issues": []})
        create_payload = json.dumps({"id": "new-id"})
        calls = [_fake_completed(stdout=list_payload), _fake_completed(stdout=create_payload),
                 _fake_completed(stdout="{}")]
        with patch("subprocess.run", side_effect=calls) as m:
            iid, result = upsert_dev({"dev_id": "proj"}, "/tmp", "M1", "标题", "todo", None)
        self.assertEqual(iid, "new-id")
        self.assertEqual(result.status, "FOUND")
        self.assertEqual(m.call_count, 3)  # list（查无） → create → status

    def test_upsert_dev_found_updates_not_create(self) -> None:
        list_payload = json.dumps({"issues": [{"id": "abc123", "title": "[M1] 旧标题"}]})
        calls = [_fake_completed(stdout=list_payload), _fake_completed(stdout="{}"),
                 _fake_completed(stdout="{}")]
        with patch("subprocess.run", side_effect=calls) as m:
            iid, result = upsert_dev({"dev_id": "proj"}, "/tmp", "M1", "新标题", "todo", None)
        self.assertEqual(iid, "abc123")
        self.assertEqual(result.status, "FOUND")
        self.assertEqual(m.call_count, 3)  # list（命中） → update → status，不含 create

    def test_upsert_dev_unknown_query_does_not_create(self) -> None:
        """UNKNOWN（超时）不得进入创建分支——此前 else 分支会把它当 NOT_FOUND 误创建。"""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=30)) as m:
            iid, result = upsert_dev({"dev_id": "proj"}, "/tmp", "M1", "标题", "todo", None)
        self.assertIsNone(iid)
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(m.call_count, 1)  # 只查询一次，未触发创建调用

    def test_upsert_todo_found_calls_set_not_add(self) -> None:
        """FOUND 分支必须真的发一次更新调用，且写后回读确认响应 stage 与期望一致。"""
        list_payload = json.dumps([{"id": 7}])
        set_payload = json.dumps({"id": 7, "stage": "active"})
        with patch("subprocess.run", side_effect=[_fake_completed(stdout=list_payload),
                                                     _fake_completed(stdout=set_payload)]) as m:
            result = upsert_todo({"main_id": "dp", "ns": "N"}, "M1", "新标题", "active", None, None, cwd="/tmp")
        self.assertEqual(result.status, "FOUND")
        self.assertEqual(result.id, 7)
        self.assertIsNone(result.error)
        self.assertEqual(m.call_count, 2)
        set_call_args = m.call_args_list[1][0][0]
        self.assertIn("set", set_call_args)

    def test_upsert_todo_found_but_patch_response_missing_stage_not_silently_success(self) -> None:
        """第 6 轮实测抓到：PATCH 响应缺 stage 字段时，旧逻辑把"缺失"当"None 即放行"
        宣称成功；缺失与不一致必须同等对待——都不能确认更新已生效。"""
        list_payload = json.dumps([{"id": 7}])
        with patch("subprocess.run", side_effect=[_fake_completed(stdout=list_payload),
                                                     _fake_completed(stdout="{}")]):
            result = upsert_todo({"main_id": "dp", "ns": "N"}, "M1", "新标题", "active", None, None, cwd="/tmp")
        self.assertIsNotNone(result.error)
        self.assertIn("缺 stage", result.error)

    def test_upsert_todo_not_found_calls_add(self) -> None:
        with patch("subprocess.run", side_effect=[_fake_completed(stdout="[]"),
                                                     _fake_completed(stdout=json.dumps({"id": 9}))]) as m:
            result = upsert_todo({"main_id": "dp", "ns": "N"}, "M1", "标题", "active", None, None, cwd="/tmp")
        self.assertEqual(result.status, "FOUND")
        self.assertEqual(result.id, 9)
        add_call_args = m.call_args_list[1][0][0]
        self.assertIn("add", add_call_args)

    def test_upsert_todo_unknown_query_does_not_create(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=30)) as m:
            result = upsert_todo({"main_id": "dp", "ns": "N"}, "M1", "标题", "active", None, None, cwd="/tmp")
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(m.call_count, 1)

    def test_find_todo_by_source_ref_found_and_not_found(self) -> None:
        with patch("subprocess.run", return_value=_fake_completed(stdout=json.dumps([{"id": 7}]))):
            self.assertEqual(find_todo_by_source_ref("ref", "/tmp").status, "FOUND")
        with patch("subprocess.run", return_value=_fake_completed(stdout=json.dumps([]))):
            self.assertEqual(find_todo_by_source_ref("ref", "/tmp").status, "NOT_FOUND")

    def test_find_todo_by_source_ref_does_not_pass_project_flag(self) -> None:
        """回归防护：dash.py todo list 无 --project 参数，传入会被 argparse 拒绝（第 4 轮实测抓到）。"""
        with patch("subprocess.run", return_value=_fake_completed(stdout="[]")) as m:
            find_todo_by_source_ref("ref", "/tmp")
        called_args = m.call_args[0][0]
        self.assertNotIn("--project", called_args)
        self.assertIn("--source-ref", called_args)


if __name__ == "__main__":
    unittest.main()
