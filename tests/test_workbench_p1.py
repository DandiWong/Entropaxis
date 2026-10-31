"""Tech-175 M1-4 契约：P1 首页数据与「待我拍板」六类卡片。

依据：`02_方案.md` 4.2 P1 元素清单、11.3「待我拍板」判据、Q-12（系统胶囊归属）、
Q-6 / D-11（租约到期口径）；Spec 4.2 的 M1-4 行与 4.3 AC 对应表（AC-13、AC-20）。

覆盖：
- P1 卡片与 `capsule_index --me` / `todo_index --me` 的输出一致（除服务端叠加字段）。
- 系统胶囊（`project == "_root"`）显示「系统」+ 灰色「不同步」（AC-20）。
- 「待我拍板」按 11.3 六类出卡，判据复用 M1-9 的 `pending_gates()` 纯函数。
- 状态栏「即将到期 N / 已过期 N」：本轮无远端（无 `remotes`）时该段不显示（Q-6 / D-11）。

测试只起服务、只读工作区文件，不写任何数据文件。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

WORKBENCH = SYSTEM_ROOT / "tools" / "workbench.py"
TOKEN_HEADER = "X-Entropaxis-Token"
_TODAY = __import__("datetime").date.today().strftime("%Y%m%d")

GATE_KINDS = ("prototype_review", "preview_review", "proposal_review",
              "audit_fuse", "worktime_review", "dispatch_failed")


def _run(argv: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *argv], capture_output=True, text=True, timeout=timeout)


def _capsule_yaml(cid: str, topic: str, extra: dict | None = None) -> str:
    import yaml
    payload = {"id": cid, "topic": topic, "mode": "full", "lifecycle": "draft",
               "seq_next": 1, "task_segments": [], "worklogs": []}
    payload.update(extra or {})
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def _raw_capsule_yaml(cid: str, topic: str, body: dict) -> str:
    """写「原样胶囊」：`roles_manifest.assignments[].attempts[]` 保留完整时间戳。

    `capsule_index()` 的 `assignments[]` 投影只留 attempts 计数（M0 契约），
    `pending_gates()` 判据需要原始 attempts；因此判据测试直接写完整胶囊文件。
    """
    import yaml
    payload = {"id": cid, "topic": topic, "mode": "full", "lifecycle": "draft",
               "seq_next": 1, "task_segments": [], "worklogs": []}
    payload.update(body)
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


class _WorkspaceCase(unittest.TestCase):
    """带临时工作区的测试基类：自带 .entropaxis 骨架与一份系统胶囊。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "ws"
        self.addCleanup(self._td.cleanup)
        (self.root / ".entropaxis" / "data" / "templates").mkdir(parents=True)
        (self.root / ".entropaxis" / "tools").mkdir(parents=True)
        self.cap_dir = self.root / "项目" / "docs" / f"{_TODAY}_某主题"
        self.cap_dir.mkdir(parents=True)
        self.cap_dir.joinpath("capsule.yaml").write_text(
            _capsule_yaml("Tech-9", "某主题"), encoding="utf-8")

    def start_server(self, me: str | None = None) -> tuple[subprocess.Popen, str, str]:
        argv = [sys.executable, str(WORKBENCH), "--port", "0", "--no-open",
                "--root", str(self.root)]
        if me:
            argv += ["--me", me]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop, proc)
        token = base = ""
        for line in proc.stderr:
            line = line.rstrip("\n")
            if line.startswith("token "):
                token = line.split(" ", 1)[1]
            if line.startswith("listening "):
                base = line.split(" ", 1)[1]
                break
        assert token and base, f"服务未就绪: token={token!r} base={base!r}"
        return proc, base, token

    @staticmethod
    def _stop(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - 兜底强停
                proc.kill()
                proc.wait(timeout=10)

    def _get(self, url: str, token: str | None = "use") -> tuple[int, object]:
        headers = {}
        if token == "use":
            headers[TOKEN_HEADER] = self.token
        elif token:
            headers[TOKEN_HEADER] = token
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8")
            try:
                return err.code, json.loads(body)
            except ValueError:
                return err.code, body


class P1DataConsistencyTests(_WorkspaceCase):
    """M1-4：P1 卡片与 `capsule_index --me` / `todo_index --me` 的输出一致。"""

    def setUp(self) -> None:
        super().setUp()
        # 归属当前胶囊给本机身份：owner 命中「我的」过滤。
        self.cap_dir.joinpath("capsule.yaml").write_text(
            _capsule_yaml("Tech-9", "某主题", {"owner": "wang.dandi"}), encoding="utf-8")
        self.proc, self.base, self.token = self.start_server(me="wang.dandi")

    def test_capsules_match_cli_json_except_view_fields(self) -> None:
        status, payload = self._get(self.base + "/api/capsules", self.token)
        self.assertEqual(status, 200, payload)
        cli = _run([str(SYSTEM_ROOT / "tools" / "aggregate_worklog.py"), "--json",
                    "--root", str(self.root), "capsule-index", "--me", "wang.dandi"])
        self.assertEqual(cli.returncode, 0, cli.stderr)
        rows = json.loads(cli.stdout)
        stripped = [{k: v for k, v in row.items()
                     if k not in ("stage_bar", "project_label", "sync_state")} for row in payload]
        self.assertEqual(stripped, rows)
        self.assertEqual([row["id"] for row in payload], ["Tech-9"])

    def test_todos_match_cli_json(self) -> None:
        # todo_index 按项目 docs/ 下的 Tasks.md 取 Active 项（M0 契约，见 test_workbench_contract）
        (self.root / "项目" / "docs" / "Tasks.md").write_text(
            "## Active\n- 写 Spec\n", encoding="utf-8")
        status, payload = self._get(self.base + "/api/todos", self.token)
        self.assertEqual(status, 200, payload)
        cli = _run([str(SYSTEM_ROOT / "tools" / "aggregate_worklog.py"), "--json",
                    "--root", str(self.root), "todo-index", "--me", "wang.dandi"])
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(payload, json.loads(cli.stdout))
        self.assertEqual([g["source"] for g in payload], ["tasks_md"])
        self.assertEqual(payload[0]["items"][0]["text"], "写 Spec")

    def test_week_kpi_matches_worklog_route(self) -> None:
        """本周投入三数与 /api/worklog 同源：人工按 effective_seconds，机时按 machine_seconds。"""
        status, worklog = self._get(self.base + "/api/worklog", self.token)
        self.assertEqual(status, 200, worklog)
        self.assertAlmostEqual(worklog["effective_seconds"] / 3600, 0.0, places=6)
        self.assertEqual(worklog["machine_seconds"], 0)
        self.assertEqual(set(worklog["by_actor"]), {"role", "manager", "human", "unlabeled"})

    def test_system_capsule_shows_system_and_no_sync(self) -> None:
        """AC-20：`project == "_root"` 的行给「系统」标签与「不同步」状态（灰色由前端渲染）。"""
        sys_dir = self.root / ".entropaxis" / "data" / "docs" / f"{_TODAY}_系统胶囊"
        sys_dir.mkdir(parents=True)
        sys_dir.joinpath("capsule.yaml").write_text(
            _capsule_yaml("Tech-900", "系统胶囊", {"owner": "wang.dandi"}), encoding="utf-8")
        status, payload = self._get(self.base + "/api/capsules", self.token)
        self.assertEqual(status, 200, payload)
        rows = {row["id"]: row for row in payload}
        self.assertIn("Tech-900", rows)
        self.assertEqual(rows["Tech-900"]["project"], "_root")
        self.assertEqual(rows["Tech-900"]["project_label"], "系统")
        self.assertEqual(rows["Tech-900"]["sync_state"], "no_sync")
        # 非系统胶囊不回填「系统」标签（未登记目录归 `_unregistered`）
        self.assertEqual(rows["Tech-9"]["project_label"], "_unregistered")
        self.assertEqual(rows["Tech-9"]["sync_state"], "local")

    def test_me_filter_hits_owner_members_and_operator_paths(self) -> None:
        """P1「我的」= owner / members / operator 经 handles 映射三条命中路径（方案 6.3）。"""
        member_dir = self.root / "项目" / "docs" / f"{_TODAY}_成员命中"
        member_dir.mkdir(parents=True)
        member_dir.joinpath("capsule.yaml").write_text(
            _capsule_yaml("Tech-8", "成员命中", {"members": ["wang.dandi"]}), encoding="utf-8")
        other_dir = self.root / "项目" / "docs" / f"{_TODAY}_他人胶囊"
        other_dir.mkdir(parents=True)
        other_dir.joinpath("capsule.yaml").write_text(
            _capsule_yaml("Tech-7", "他人胶囊", {"owner": "li.na"}), encoding="utf-8")
        status, payload = self._get(self.base + "/api/capsules", self.token)
        self.assertEqual(status, 200, payload)
        ids = sorted(row["id"] for row in payload)
        self.assertEqual(ids, ["Tech-8", "Tech-9"])


class GatesRouteTests(_WorkspaceCase):
    """M1-4：「待我拍板」六类卡片（方案 11.3）。"""

    def setUp(self) -> None:
        super().setUp()
        self.proc, self.base, self.token = self.start_server(me="wang.dandi")

    def _write(self, cid: str, topic: str, body: dict) -> None:
        cap_dir = self.root / "项目" / "docs" / f"{_TODAY}_{cid}"
        cap_dir.mkdir(parents=True, exist_ok=True)
        cap_dir.joinpath("capsule.yaml").write_text(
            _raw_capsule_yaml(cid, topic, body), encoding="utf-8")

    def _attempt(self, started: str, ended: str, ok: bool = True) -> dict:
        return {"preference_number": 1, "started_at": started, "ended_at": ended,
                "exit_code": 0 if ok else 1}

    def test_gates_route_returns_list_of_six_kinds(self) -> None:
        status, payload = self._get(self.base + "/api/gates", self.token)
        self.assertEqual(status, 200, payload)
        self.assertIsInstance(payload, list)
        for card in payload:
            self.assertIn(card["kind"], GATE_KINDS)
            self.assertIn("capsule_id", card)

    def test_prototype_review_card_when_designer_succeeded_and_no_next(self) -> None:
        """原型待确认：Designer succeeded 且之后本胶囊无 attempt 开始（11.3 原型行）。"""
        self._write("Tech-50", "原型待确认", {
            "lifecycle": "active", "owner": "wang.dandi",
            "roles_manifest": {"assignments": [
                {"assignment_id": "Designer-1", "role": "Designer", "status": "succeeded",
                 "deliverable": "03_设计.md",
                 "attempts": [self._attempt("2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00")]},
            ]},
        })
        status, payload = self._get(self.base + "/api/gates", self.token)
        self.assertEqual(status, 200, payload)
        kinds = {card["kind"] for card in payload}
        self.assertIn("prototype_review", kinds)
        self.assertEqual([c["capsule_id"] for c in payload if c["kind"] == "prototype_review"],
                         ["Tech-50"])

    def test_dispatch_failed_card_without_later_success(self) -> None:
        """调度失败：assignment failed 且之后无同一 assignment 的成功 attempt。"""
        self._write("Tech-51", "调度失败", {
            "lifecycle": "active", "owner": "wang.dandi",
            "roles_manifest": {"assignments": [
                {"assignment_id": "Builder-1", "role": "Builder", "status": "failed",
                 "deliverable": "04_Spec.md",
                 "attempts": [self._attempt("2026-10-01T10:00:00+08:00",
                                            "2026-10-01T11:00:00+08:00", ok=False)]},
            ]},
        })
        status, payload = self._get(self.base + "/api/gates", self.token)
        self.assertEqual(status, 200, payload)
        self.assertIn("dispatch_failed", {card["kind"] for card in payload})

    def test_no_cards_for_delivered_lifecycle(self) -> None:
        """lifecycle ∈ {delivered, archived} 不出卡（11.3 范围）。"""
        self._write("Tech-52", "已交付", {
            "lifecycle": "delivered", "owner": "wang.dandi",
            "roles_manifest": {"assignments": [
                {"assignment_id": "Designer-1", "role": "Designer", "status": "succeeded",
                 "deliverable": "03_设计.md",
                 "attempts": [self._attempt("2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00")]},
            ]},
        })
        status, payload = self._get(self.base + "/api/gates", self.token)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload, [])

    def test_gates_do_not_write_any_file(self) -> None:
        """运行全程不写文件（Spec 1.6）：取卡片前后工作区文件集合不变。"""
        before = {str(p.relative_to(self.root)): p.stat().st_mtime_ns
                  for p in self.root.rglob("*") if p.is_file()}
        self._get(self.base + "/api/gates", self.token)
        after = {str(p.relative_to(self.root)): p.stat().st_mtime_ns
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


class LeaseStatusBarTests(_WorkspaceCase):
    """M1-4：状态栏「即将到期 N / 已过期 N」本轮无远端时不显示（Q-6 / D-11）。"""

    def setUp(self) -> None:
        super().setUp()
        self.proc, self.base, self.token = self.start_server(me="wang.dandi")

    def test_route_table_has_no_lease_api(self) -> None:
        """租约状态不进 API 路由：口径由前端按 remotes 是否存在决定（本轮单机模式不显示）。"""
        import workbench
        for forbidden in ("/api/leases", "/api/lease"):
            self.assertNotIn(forbidden, workbench.READ_ROUTES)
        status, body, _ = (None, None, None)
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    self.base + "/api/leases",
                    headers={TOKEN_HEADER: self.token}), timeout=30) as resp:
                status = resp.status
        except urllib.error.HTTPError as err:
            status = err.code
            body = err.read().decode("utf-8")
        self.assertEqual(status, 404, body)

    def test_static_assets_have_no_remote_refs(self) -> None:
        """M1-3 契约在 M1-4 仍成立：前端资源零外链（前端只读服务，不直接读文件）。"""
        static = SYSTEM_ROOT / "tools" / "workbench" / "static"
        text = (static / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("http://", text.replace("http://www.w3.org", ""))
        self.assertNotIn("https://", text)
        app = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn("X-Entropaxis-Token", app)
        # 前端只读：没有任何写文件路径（写只走服务端写路由）
        self.assertNotIn("writeFile", app)
        self.assertNotIn("localStorage.setItem", app)


if __name__ == "__main__":
    unittest.main()
