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


class CapsuleIndexTests(unittest.TestCase):
    """方案 6.3：capsule_index 只读聚合层（不落盘缓存）与「我的」过滤。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.ws = self.root / "ws"
        (self.ws / ".entropaxis" / "data" / "templates").mkdir(parents=True)
        (self.ws / ".entropaxis" / "data" / "workbench").mkdir(parents=True)
        self._registry("""\
exclude: []
projects:
  - {id: alpha, name: 甲项目, path: 甲项目/}
""")
        self._stakeholders("""\
_meta: {source: t, managed_by: t, policy: merge-only}
people:
  - {id: dandi, name: Dandi, handles: {git: Dandi Wong, dashboard: dandi}}
  - {id: ling, name: Ling}
projects: {}
""")
        self.mine = self.ws / "甲项目" / "docs" / "20261030_我的事务"
        self.mine.mkdir(parents=True)
        self.other = self.ws / "甲项目" / "docs" / "20261030_他人事务"
        self.other.mkdir(parents=True)

    def _registry(self, text: str) -> None:
        (self.ws / ".entropaxis" / "data" / "templates" / "registry.yaml").write_text(text, encoding="utf-8")

    def _stakeholders(self, text: str) -> None:
        (self.ws / ".entropaxis" / "data" / "templates" / "stakeholders.yaml").write_text(text, encoding="utf-8")

    def _write(self, cap: Path, cid: str, topic: str, *, owner=None, members=None,
               assignments=(), worklogs=()) -> None:
        import yaml
        manifest = {
            "id": cid, "topic": topic, "mode": "full", "lifecycle": "active", "seq_next": 1,
            "task_segments": [], "worklogs": list(worklogs),
            "roles_manifest": {"assigned_at": "2026-10-30T00:00:00+08:00",
                               "assigned_by": "t", "revision": 1, "assignments": list(assignments)},
        }
        if owner is not None:
            manifest["owner"] = owner
        if members is not None:
            manifest["members"] = members
        (cap / "capsule.yaml").write_text(
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def test_index_lists_capsules_with_required_fields(self) -> None:
        """输出含 id/topic/项目/lifecycle/owner/members/open 段/指派/工时与 token 合计。"""
        self._write(self.mine, "Tech-9", "我的事务", owner="dandi",
                    assignments=[{"assignment_id": "a1", "role": "Builder", "command_profile": "builder",
                                  "status": "succeeded", "deliverable": "04_Spec_Tech-9.md",
                                  "attempts": [{"profile": "builder", "argv_sha256": "a" * 64,
                                                "started_at": "2026-10-30T10:00:00+08:00",
                                                "ended_at": "2026-10-30T10:07:00+08:00",
                                                "usage": {"session_id": "s", "models": {"m": {"in": 100, "out": 20,
                                                                                            "cache_read": 5,
                                                                                            "cache_write": 1}},
                                                          "cost_usd": 0.5}}]}],
                    worklogs=[{"date": "2026-10-30", "stage": "04_实施", "spent": 0.1167, "seconds": 420,
                               "actor": "role", "role": "Builder", "summary": "写 Spec",
                               "operator": "Dandi Wong", "ref": "Tech-9#1"}])
        rows = AW.capsule_index(self.ws.resolve(), "2026-10-01", "2026-10-30")
        self.assertEqual([r["id"] for r in rows], ["Tech-9"])
        row = rows[0]
        for key in ("id", "topic", "project", "lifecycle", "owner", "members", "open_segments",
                    "assignments", "worklog_seconds", "machine_seconds", "usage", "path"):
            self.assertIn(key, row, f"capsule_index 输出须含 {key}")
        self.assertEqual((row["project"], row["owner"]), ("alpha", "dandi"))
        self.assertEqual(row["usage"]["role"]["in"], 100)
        self.assertEqual(row["usage"]["total"]["in"], 100)
        self.assertEqual(row["usage"]["unknown_count"], 0)
        self.assertEqual(row["worklog_seconds"], 420)

    def test_owner_falls_back_to_first_non_role_segment_operator(self) -> None:
        """缺 owner 时按首个 actor≠role 的段 operator 经 handles.git 映射推导，只展示不回写。"""
        self._write(self.mine, "Tech-8", "旧胶囊", assignments=[],
                    worklogs=[{"date": "2026-10-30", "stage": "misc", "spent": 0.1, "seconds": 360,
                               "summary": "旧段", "operator": "Dandi Wong", "ref": "Tech-8#1"}])
        import yaml
        data = yaml.safe_load((self.mine / "capsule.yaml").read_text(encoding="utf-8"))
        data["task_segments"] = [{"seq": 1, "seg_id": "Tech-8#1", "name": "事务段",
                                  "startAt": "2026-10-30T09:00:00+08:00",
                                  "lastActivityAt": "2026-10-30T09:06:00+08:00",
                                  "state": "logged", "actor": "manager"}]
        (self.mine / "capsule.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        [row] = AW.capsule_index(self.ws.resolve(), "2026-10-01", "2026-10-30")
        self.assertEqual(row["owner"], "dandi")
        self.assertEqual(row["owner_source"], "derived")
        # 推导不回写：磁盘上的胶囊文件没有 owner 字段
        self.assertIsNone(yaml.safe_load((self.mine / "capsule.yaml").read_text(encoding="utf-8")).get("owner"))

    def test_mine_filter_matches_owner_members_or_operator(self) -> None:
        """三条命中路径：owner=me、me∈members、operator 经 handles 映射到 me。"""
        self._write(self.mine, "Tech-9", "我的事务", owner="dandi")
        self._write(self.other, "Tech-10", "成员事务", members=["dandi"])
        third = self.ws / "甲项目" / "docs" / "20261030_登记事务"
        third.mkdir(parents=True)
        self._write(third, "Tech-11", "登记事务", worklogs=[
            {"date": "2026-10-30", "stage": "misc", "spent": 0.1, "seconds": 360,
             "summary": "s", "operator": "Dandi Wong", "ref": "Tech-11#1"}])
        fourth = self.ws / "甲项目" / "docs" / "20261030_无关事务"
        fourth.mkdir(parents=True)
        self._write(fourth, "Tech-12", "无关事务", owner="ling")
        ids = [r["id"] for r in AW.capsule_index(self.ws.resolve(), "2026-10-01", "2026-10-30", me="dandi")]
        self.assertEqual(sorted(ids), ["Tech-10", "Tech-11", "Tech-9"])
        self.assertEqual(sorted(r["id"] for r in AW.capsule_index(self.ws.resolve(), "2026-10-01", "2026-10-30")),
                         ["Tech-10", "Tech-11", "Tech-12", "Tech-9"])

    def test_usage_manager_subtotal_dedupes_session_turns(self) -> None:
        """Manager 会话小计只取绑定该胶囊的轮次，同一 (session_id, turn_seq) 重复行只计一次。"""
        self._write(self.mine, "Tech-9", "我的事务", owner="dandi")
        import json
        rows = [
            {"session_id": "s1", "turn_seq": 1, "capsule_id": "Tech-9",
             "ended_at": "2026-10-30T11:00:00+08:00",
             "usage": {"models": {"m": {"in": 40, "out": 4, "cache_read": 0, "cache_write": 0}}}},
            {"session_id": "s1", "turn_seq": 1, "capsule_id": "Tech-9",
             "ended_at": "2026-10-30T11:00:00+08:00",
             "usage": {"models": {"m": {"in": 40, "out": 4, "cache_read": 0, "cache_write": 0}}}},
            {"session_id": "s1", "turn_seq": 2, "capsule_id": "Tech-10",
             "ended_at": "2026-10-30T11:05:00+08:00",
             "usage": {"models": {"m": {"in": 99, "out": 0, "cache_read": 0, "cache_write": 0}}}},
            {"session_id": "s1", "turn_seq": 3, "capsule_id": None,
             "ended_at": "2026-10-30T11:06:00+08:00",
             "usage": {"models": {"m": {"in": 7, "out": 0, "cache_read": 0, "cache_write": 0}}}},
        ]
        (self.ws / ".entropaxis" / "data" / "workbench" / "sessions.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        [row] = AW.capsule_index(self.ws.resolve(), "2026-10-01", "2026-10-30")
        self.assertEqual(row["usage"]["manager"]["in"], 40)
        self.assertEqual(row["usage"]["total"]["in"], 40)
        self.assertEqual(row["usage"]["unknown_count"], 0)

    def test_unknown_usage_counted_not_zeroed(self) -> None:
        """usage == unknown 的 attempt 不计数值，单列 unknown_count（方案 6.5.1）。"""
        self._write(self.mine, "Tech-9", "我的事务", owner="dandi",
                    assignments=[{"assignment_id": "a1", "role": "Builder", "command_profile": "builder",
                                  "status": "failed", "deliverable": "04_Spec_Tech-9.md",
                                  "attempts": [{"profile": "builder", "argv_sha256": "a" * 64,
                                                "started_at": "2026-10-30T10:00:00+08:00",
                                                "ended_at": "2026-10-30T10:07:00+08:00",
                                                "usage": "unknown"}]}])
        [row] = AW.capsule_index(self.ws.resolve(), "2026-10-01", "2026-10-30")
        self.assertEqual(row["usage"]["unknown_count"], 1)
        self.assertEqual(row["usage"]["role"]["in"], 0)
        self.assertEqual(row["usage"]["total"]["in"], 0)

    def test_open_segments_and_assignments_are_listed(self) -> None:
        import yaml
        self._write(self.mine, "Tech-9", "我的事务", owner="dandi",
                    assignments=[{"assignment_id": "a1", "role": "Reviewer", "command_profile": "reviewer",
                                  "status": "pending"}])
        data = yaml.safe_load((self.mine / "capsule.yaml").read_text(encoding="utf-8"))
        data["task_segments"] = [{"seq": 1, "seg_id": "Tech-9#1", "name": "Builder→04_Spec",
                                  "startAt": "2026-10-30T09:00:00+08:00",
                                  "lastActivityAt": "2026-10-30T09:06:00+08:00",
                                  "state": "open", "actor": "role", "role": "Builder",
                                  "dispatch_id": "a" * 32, "stage": "04_实施"}]
        (self.mine / "capsule.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        [row] = AW.capsule_index(self.ws.resolve(), "2026-10-01", "2026-10-30")
        self.assertEqual([s["seg_id"] for s in row["open_segments"]], ["Tech-9#1"])
        self.assertEqual(row["open_segments"][0]["actor"], "role")
        self.assertEqual([a["role"] for a in row["assignments"]], ["Reviewer"])

    def test_index_writes_no_cache_file(self) -> None:
        """沿用 find_capsule.py:4-10 理由：不落盘索引，每次实时扫描。"""
        self._write(self.mine, "Tech-9", "我的事务", owner="dandi")
        AW.capsule_index(self.ws.resolve(), "2026-10-01", "2026-10-30")
        created = sorted(str(p.relative_to(self.ws)) for p in self.ws.rglob("*")
                         if p.is_file() and p.name not in ("capsule.yaml", "registry.yaml", "stakeholders.yaml"))
        self.assertEqual(created, [])


class TodoMergeTests(unittest.TestCase):
    """方案 6.3「TODO 合并」：tasks/capsule/dispatch 三类线索 + 到期日期，按来源分组。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.ws = self.root / "ws"
        (self.ws / ".entropaxis" / "data" / "templates").mkdir(parents=True)
        (self.ws / ".entropaxis" / "data" / "templates" / "registry.yaml").write_text(
            "exclude: []\nprojects:\n  - {id: alpha, name: 甲, path: 甲/}\n", encoding="utf-8")
        (self.ws / ".entropaxis" / "data" / "templates" / "stakeholders.yaml").write_text(
            "_meta: {source: t, managed_by: t, policy: merge-only}\n"
            "people:\n  - {id: dandi, name: Dandi, handles: {git: Dandi Wong}}\nprojects: {}\n",
            encoding="utf-8")

    def test_todo_groups_by_source(self) -> None:
        import yaml
        docs = self.ws / "甲" / "docs"
        docs.mkdir(parents=True)
        (docs / "Tasks.md").write_text("## Active\n- 推进实施\n", encoding="utf-8")
        cap = docs / "20261030_我的事务"
        cap.mkdir()
        (cap / "capsule.yaml").write_text(yaml.safe_dump({
            "id": "Tech-9", "topic": "我的事务", "mode": "full", "lifecycle": "active",
            "seq_next": 2, "owner": "dandi",
            "review_due_at": "2026-11-29", "closure_deadline": "2026-12-29",
            "task_segments": [{"seq": 1, "seg_id": "Tech-9#1", "name": "Builder→Spec",
                               "startAt": "2026-10-30T09:00:00+08:00",
                               "lastActivityAt": "2026-10-30T09:06:00+08:00",
                               "state": "open", "actor": "role", "role": "Builder",
                               "dispatch_id": "a" * 32, "stage": "04_实施"}],
            "worklogs": [], "roles_manifest": {"assigned_at": "2026-10-30T00:00:00+08:00",
                                               "assigned_by": "t", "revision": 1,
                                               "assignments": [{"assignment_id": "a1", "role": "Reviewer",
                                                                "command_profile": "reviewer",
                                                                "status": "pending"}]}}, allow_unicode=True), encoding="utf-8")
        groups = AW.todo_index(self.ws.resolve(), me="dandi")
        by_source = {g["source"]: g["items"] for g in groups}
        self.assertIn("tasks_md", by_source)
        self.assertEqual(by_source["tasks_md"][0]["text"], "推进实施")
        self.assertIn("open_segment", by_source)
        self.assertEqual(by_source["open_segment"][0]["seg_id"], "Tech-9#1")
        self.assertIn("assignment_pending", by_source)
        self.assertEqual(by_source["assignment_pending"][0]["assignment_id"], "a1")
        self.assertIn("capsule_due", by_source)
        self.assertEqual(by_source["capsule_due"][0]["review_due_at"], "2026-11-29")


class CapsuleIndexCliTests(unittest.TestCase):
    """capsule-index 子命令：只读、可 --json，不改变缺省聚合路径。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.ws = self.root / "ws"
        (self.ws / ".entropaxis" / "data" / "templates").mkdir(parents=True)
        (self.ws / ".entropaxis" / "data" / "templates" / "registry.yaml").write_text(
            "exclude: []\nprojects: []\n", encoding="utf-8")
        (self.ws / ".entropaxis" / "data" / "docs" / "20261030_主题").mkdir(parents=True)
        (self.ws / ".entropaxis" / "data" / "docs" / "20261030_主题" / "capsule.yaml").write_text(
            _capsule_yaml("Tech-9", "主题"), encoding="utf-8")

    def test_capsule_index_subcommand_json(self) -> None:
        proc = _run([str(AGGREGATE), "--root", str(self.ws), "--json", "capsule-index"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual([r["id"] for r in payload], ["Tech-9"])
        self.assertEqual(payload[0]["project"], "_root")

    def test_default_aggregate_path_unchanged(self) -> None:
        proc = _run([str(AGGREGATE), "--root", str(self.ws), "--since", "2026-10-01"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("### 工时统计汇总", proc.stdout)

    def test_todo_index_subcommand_json(self) -> None:
        proc = _run([str(AGGREGATE), "--root", str(self.ws), "--json", "todo-index"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [])

    def test_project_before_subcommand_errors_instead_of_silently_ignored(self) -> None:
        """IA-5：`--project` 放在子命令前必须报错，不静默忽略。

        主解析器与子解析器曾各注册一个 `--project`，子命令缺省值 None 覆盖主解析器取值，
        导致 `--project X --json capsule-index` 返回全量且退出码 0。主解析器的 `--project`
        已删除，该位置现在由 argparse 直接报错（退出码 2，无 stdout 输出）。
        """
        proc = _run([str(AGGREGATE), "--root", str(self.ws), "--project", "alpha",
                     "--json", "capsule-index"])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, "", "报错路径不得输出任何数据")
        self.assertIn("invalid choice", proc.stderr)

    def test_project_after_subcommand_filters(self) -> None:
        """IA-5：`--project` 放在子命令后仍按文档形式生效（Spec 1.4）。"""
        proc = _run([str(AGGREGATE), "--root", str(self.ws), "--json", "capsule-index",
                     "--project", "_root"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual([r["id"] for r in payload], ["Tech-9"])

    def test_default_path_has_no_project_option(self) -> None:
        """IA-5：缺省聚合路径不再接受 `--project`（该路径只有 `--operator` 过滤）。"""
        proc = _run([str(AGGREGATE), "--root", str(self.ws), "--project", "alpha"])
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("invalid choice", proc.stderr)


if __name__ == "__main__":
    unittest.main()
