"""Tech-175 M0 契约：三工具 --json 输出层与 capsule_index 只读聚合层。

依据：data/docs/20261029_Entropaxis团队协作工作台/02_方案.md 6.3（--json、胶囊索引、
「我的」过滤、TODO 合并）与第九节 M0。只验输出层与聚合层，不连网、不落盘缓存。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

import aggregate_worklog as AW  # noqa: E402
import find_capsule as FC  # noqa: E402
import resume_context as RC  # noqa: E402

FIND_CAPSULE = SYSTEM_ROOT / "tools" / "find_capsule.py"
AGGREGATE = SYSTEM_ROOT / "tools" / "aggregate_worklog.py"
RESUME = SYSTEM_ROOT / "tools" / "resume_context.py"


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *argv], capture_output=True, text=True, timeout=120)


def _capsule_yaml(cid: str, topic: str, *, owner=None, members=None, worklogs=()) -> str:
    import yaml
    payload = {"id": cid, "topic": topic, "mode": "full", "lifecycle": "draft",
               "seq_next": 1, "task_segments": [], "worklogs": list(worklogs)}
    if owner is not None:
        payload["owner"] = owner
    if members is not None:
        payload["members"] = members
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


class ToolJsonFlagTests(unittest.TestCase):
    """三工具 --json：只加输出层，既有文本行为与退出码不变。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def _workspace(self) -> Path:
        ws = self.root / "ws"
        (ws / ".entropaxis" / "data" / "templates").mkdir(parents=True)
        (ws / "项目" / "docs" / "20261030_某主题").mkdir(parents=True)
        (ws / "项目" / "docs" / "20261030_某主题" / "capsule.yaml").write_text(
            _capsule_yaml("Tech-9", "某主题"), encoding="utf-8")
        return ws

    def test_find_capsule_json_matches_text_results(self) -> None:
        ws = self._workspace()
        text = _run([str(FIND_CAPSULE), "某主题", "--root", str(ws)])
        self.assertEqual(text.returncode, 0, text.stderr)
        as_json = _run([str(FIND_CAPSULE), "某主题", "--root", str(ws), "--json"])
        self.assertEqual(as_json.returncode, 0, as_json.stderr)
        payload = json.loads(as_json.stdout)
        self.assertEqual(payload["results"], text.stdout.split())
        self.assertEqual(payload["count"], 1)

    def test_find_capsule_json_error_stays_stderr_and_exit_1(self) -> None:
        """未命中：JSON 只描述已成功的检索，失败仍走 stderr 与非零退出码。"""
        ws = self._workspace()
        proc = _run([str(FIND_CAPSULE), "不存在的词", "--root", str(ws), "--json"])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("未命中", proc.stderr)

    def test_resume_context_json_matches_render(self) -> None:
        ws = self._workspace()
        (ws / "项目" / "docs" / "Tasks.md").write_text(
            "## Active\n- 写 Spec\n", encoding="utf-8")
        text = _run([str(RESUME), "--root", str(ws)])
        self.assertEqual(text.returncode, 0, text.stderr)
        as_json = _run([str(RESUME), "--root", str(ws), "--json"])
        self.assertEqual(as_json.returncode, 0, as_json.stderr)
        payload = json.loads(as_json.stdout)
        self.assertEqual(payload["verdict"], RC.resume_context(
            ws.resolve(), workspace=ws.resolve())["verdict"])
        self.assertEqual(payload["signals"]["tasks"][0]["active"], ["写 Spec"])
        # render() 是人读层：JSON 是同一 dict，键集合一致
        self.assertEqual(set(payload), {"verdict", "projects", "signals"})

    def test_aggregate_json_markdown_and_seconds_keys(self) -> None:
        ws = self._workspace()
        proc = _run([str(AGGREGATE), "--root", str(ws), "--since", "2026-10-01", "--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        data = AW.aggregate(ws.resolve(), "2026-10-01", "2026-10-30")
        self.assertEqual(payload["total_hours"], data["total_hours"])
        for key in ("total_seconds", "effective_seconds", "machine_seconds", "by_actor", "items"):
            self.assertIn(key, payload, f"--json 须含 {key}")
        self.assertEqual(set(payload["by_actor"]), {"role", "manager", "human", "unlabeled"})

    def test_json_flag_does_not_change_default_output(self) -> None:
        """缺省仍是文本：程序读者按需加 --json，人读路径不变。"""
        ws = self._workspace()
        text = _run([str(AGGREGATE), "--root", str(ws), "--since", "2026-10-01"])
        self.assertIn("### 工时统计汇总", text.stdout)


if __name__ == "__main__":
    unittest.main()
