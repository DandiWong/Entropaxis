"""Tech-175 M1-5 契约：P2 会话页数据（阶段条 / 当前角色卡 / 运行中徽标 / 用量小计）。

依据：`02_方案.md` 4.2 P2 行（阶段条判据 11.2、当前角色卡取 run 文件、本会话用量两小计
6.5.1）、Q-4 / AC-12（徽标胶囊 ID 读 `<capsule>/capsule.yaml::id`，`capsule=null` 显示
「未绑定胶囊」，不从 cwd 或交付物路径反推）、Q-8 / D-13（执行端三态，M4 前服务端置灰）。

测试只起本机服务、只读临时工作区文件，不发外网请求、不落盘缓存。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

WORKBENCH = SYSTEM_ROOT / "tools" / "workbench.py"
STATIC_DIR = SYSTEM_ROOT / "tools" / "workbench" / "static"
TOKEN_HEADER = "X-Entropaxis-Token"
_TODAY = __import__("datetime").date.today().strftime("%Y%m%d")

STAGE_KEYS = ["01_调研", "02_方案", "03_设计", "04_Spec", "05_审计", "07_验收", "08_汇报"]


def _capsule_yaml(cid: str, topic: str, extra: dict | None = None) -> str:
    import yaml
    payload = {"id": cid, "topic": topic, "mode": "full", "lifecycle": "draft",
               "seq_next": 1, "task_segments": [], "worklogs": []}
    payload.update(extra or {})
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def _at(day: int, hour: int = 10) -> str:
    """本周第 day 天（0=周一）+08:00 的时刻：fixture 相对当前周生成，不写死日期（IA-1）。"""
    import datetime as dt
    import workbench_view
    monday = dt.date.fromisoformat(workbench_view.week_since())
    moment = dt.datetime.combine(monday + dt.timedelta(days=day), dt.time(hour, 0),
                                 tzinfo=dt.timezone(dt.timedelta(hours=8)))
    return moment.isoformat()


class _WorkspaceCase(unittest.TestCase):
    """带临时工作区的测试基类：自带 .entropaxis 骨架与一份胶囊。"""

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

    def _write_run(self, name: str, payload: dict) -> None:
        run_dir = self.root / ".entropaxis" / "data" / "logs" / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class RunCapsuleIdTests(_WorkspaceCase):
    """AC-12 / Q-4：运行中徽标的胶囊 ID 读 `<capsule>/capsule.yaml::id`。"""

    def setUp(self) -> None:
        super().setUp()
        self.proc, self.base, self.token = self.start_server()

    def test_capsule_id_read_from_capsule_yaml_not_cwd(self) -> None:
        """胶囊 ID 取自 `capsule` 指向目录的 capsule.yaml，与 cwd / 交付物路径无关。"""
        other = self.root / "别处" / "20261001_别的胶囊"
        other.mkdir(parents=True)
        (other / "capsule.yaml").write_text(
            _capsule_yaml("Tech-404", "别处胶囊"), encoding="utf-8")
        self._write_run("run-a.1.json", {
            "dispatch_id": "run-a.1", "pid": os.getpid(), "role": "Designer",
            "profile": "designer-1", "agent": "claude", "model": "opus",
            "started_at": "2026-10-30T09:00:00+00:00",
            "cwd": str(other),                      # cwd 指向另一颗胶囊
            "capsule": str(self.cap_dir),           # 实际胶囊才是徽标来源
            "preference_number": 1, "timeout_s": 1800,
        })
        status, payload = self._get(self.base + "/api/runs", self.token)
        self.assertEqual(status, 200, payload)
        row = next(r for r in payload if r["dispatch_id"] == "run-a.1")
        self.assertEqual(row["capsule_id"], "Tech-9")

    def test_null_capsule_yields_null_capsule_id(self) -> None:
        """`capsule = null` 时胶囊 ID 为 null：前端显示「未绑定胶囊」，不反推。"""
        self._write_run("run-b.1.json", {
            "dispatch_id": "run-b.1", "pid": os.getpid(), "role": "Builder",
            "profile": "builder-3", "agent": "omp", "model": "m",
            "started_at": "2026-10-30T09:00:00+00:00",
            "cwd": str(self.cap_dir), "capsule": None,
            "preference_number": 2, "timeout_s": 3600,
        })
        status, payload = self._get(self.base + "/api/runs", self.token)
        self.assertEqual(status, 200, payload)
        row = next(r for r in payload if r["dispatch_id"] == "run-b.1")
        self.assertIsNone(row["capsule_id"])

    def test_missing_capsule_key_yields_null_capsule_id(self) -> None:
        """旧 run 文件没有 `capsule` 键：同样按未绑定处理，路由不报错。"""
        self._write_run("run-c.1.json", {
            "dispatch_id": "run-c.1", "pid": os.getpid(), "role": "Reviewer",
            "profile": "reviewer-1", "agent": "omp", "model": "m",
            "started_at": "2026-10-30T09:00:00+00:00",
        })
        status, payload = self._get(self.base + "/api/runs", self.token)
        self.assertEqual(status, 200, payload)
        row = next(r for r in payload if r["dispatch_id"] == "run-c.1")
        self.assertIsNone(row["capsule_id"])

    def test_unreadable_capsule_dir_yields_null_capsule_id(self) -> None:
        """`capsule` 指向不存在的目录：不推断、不抛错，按未绑定处理。"""
        self._write_run("run-d.1.json", {
            "dispatch_id": "run-d.1", "pid": os.getpid(), "role": "Builder",
            "profile": "builder-3", "agent": "omp", "model": "m",
            "started_at": "2026-10-30T09:00:00+00:00",
            "capsule": str(self.root / "不存在" / "目录"),
        })
        status, payload = self._get(self.base + "/api/runs", self.token)
        self.assertEqual(status, 200, payload)
        row = next(r for r in payload if r["dispatch_id"] == "run-d.1")
        self.assertIsNone(row["capsule_id"])

    def test_role_card_fields_available_on_run_rows(self) -> None:
        """当前角色卡五要素都在 run 行上：角色 / profile / agent·model / 状态 / 偏好序号。"""
        self._write_run("run-e.1.json", {
            "dispatch_id": "run-e.1", "pid": os.getpid(), "role": "Designer",
            "profile": "designer-1", "agent": "claude", "model": "opus",
            "started_at": "2026-10-30T09:00:00+00:00",
            "capsule": str(self.cap_dir), "preference_number": 1, "timeout_s": 1800,
        })
        status, payload = self._get(self.base + "/api/runs", self.token)
        self.assertEqual(status, 200, payload)
        row = next(r for r in payload if r["dispatch_id"] == "run-e.1")
        for key in ("role", "profile", "agent", "model", "alive",
                    "preference_number", "timeout_s", "capsule_id", "started_at"):
            self.assertIn(key, row)
        self.assertEqual(row["role"], "Designer")
        self.assertEqual(row["preference_number"], 1)
        self.assertTrue(row["alive"])


class MeIdentityTests(_WorkspaceCase):
    """M1-5：本机身份缺省时不把「无身份」当成一个人 id。"""

    def setUp(self) -> None:
        super().setUp()
        self.proc, self.base, self.token = self.start_server()

    def test_absent_me_key_does_not_filter_capsules(self) -> None:
        """`workspace-config.yaml` 无 `me` 键时不得退化成字符串 "None"。

        否则服务给 `capsule-index` 传 `--me None`，把全部胶囊过滤成空表（首页无数据）。
        """
        import workbench
        self.assertIsNone(workbench._read_me(self.root))
        status, payload = self._get(self.base + "/api/capsules", self.token)
        self.assertEqual(status, 200, payload)
        self.assertEqual([r["id"] for r in payload], ["Tech-9"])

    def test_me_key_is_used_as_identity(self) -> None:
        """有 `me` 键时按该 id 过滤（Spec 1.2）。"""
        config = self.root / ".entropaxis" / "data" / "templates" / "workspace-config.yaml"
        config.write_text("me: wang.dandi\n", encoding="utf-8")
        import workbench
        self.assertEqual(workbench._read_me(self.root), "wang.dandi")


class P2StageBarTests(_WorkspaceCase):
    """M1-5：P2 阶段条与服务端 `stage_bar()` 同源，六态齐备且当前两态不同色。"""

    def setUp(self) -> None:
        super().setUp()
        self.cap_dir.joinpath("capsule.yaml").write_text(_capsule_yaml(
            "Tech-9", "某主题", {"owner": "wang.dandi"}), encoding="utf-8")
        self.proc, self.base, self.token = self.start_server(me="wang.dandi")

    def test_capsules_route_stage_bar_matches_pure_function(self) -> None:
        """阶段条由服务端算好随行下发：与 `workbench_view.stage_bar()` 逐项相等。"""
        import yaml
        import workbench
        import workbench_view
        runs = [{"dispatch_id": "abc", "pid": os.getpid()}]
        status, payload = self._get(self.base + "/api/capsules", self.token)
        self.assertEqual(status, 200, payload)
        raw = yaml.safe_load((self.cap_dir / "capsule.yaml").read_text(encoding="utf-8"))
        expected = workbench_view.stage_bar(raw, runs=runs)
        row = next(r for r in payload if r["id"] == "Tech-9")
        self.assertEqual([s["stage"] for s in row["stage_bar"]], STAGE_KEYS)
        self.assertEqual(row["stage_bar"], expected)

    def test_stage_bar_residual_state_uses_amber_tone(self) -> None:
        """11.2：open 角色段 + pid 已死 → 「残留待处置」（amber），与运行中（brand）不同色。"""
        import workbench_view
        seg = {"seg_id": "Tech-9#1", "name": "设计·Designer", "state": "open",
               "actor": "role", "role": "Designer", "stage": "03_设计",
               "dispatch_id": "deadbeef", "startAt": "2026-11-01T10:00:00+08:00"}
        dead_run = {"dispatch_id": "deadbeef.1", "pid": 2 ** 22 + 1}
        bar = workbench_view.stage_bar({"task_segments": [seg]}, runs=[dead_run])
        current = [s for s in bar if s["state"] in ("running", "residual", "parked")]
        self.assertEqual([s["state"] for s in current], ["residual"])
        self.assertEqual(current[0]["tone"], "amber")
        # 同一胶囊的运行态由服务端注入：活进程 pid 才判 running
        self.cap_dir.joinpath("capsule.yaml").write_text(_capsule_yaml(
            "Tech-9", "某主题", {"owner": "wang.dandi", "task_segments": [seg]}),
            encoding="utf-8")
        self._write_run("run-dead.1.json", {
            "dispatch_id": "deadbeef.1", "pid": 2 ** 22 + 1, "role": "Designer",
            "profile": "designer-1", "agent": "claude", "model": "opus",
            "started_at": "2026-11-01T10:00:00+08:00", "capsule": str(self.cap_dir),
            "preference_number": 1, "timeout_s": 1800,
        })
        status, payload = self._get(self.base + "/api/capsules", self.token)
        self.assertEqual(status, 200, payload)
        row = next(r for r in payload if r["id"] == "Tech-9")
        states = {s["stage"]: s for s in row["stage_bar"]}
        self.assertEqual(states["03_设计"]["state"], "residual")
        self.assertEqual(states["03_设计"]["tone"], "amber")

    def test_app_js_renders_six_states_with_distinct_current_tones(self) -> None:
        """前端六态齐备；「当前·运行中」与「当前·残留待处置」用不同色（11.2）。"""
        app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        for state in ("running", "residual", "parked", "done", "todo", "uninstantiated"):
            self.assertIn(f"{state}:", app)
        self.assertNotEqual("brand", "res")
        self.assertIn("STATE_TONE", app)
        # 阶段条渲染复用服务端 stage_bar，不在前端重算判据
        self.assertIn("workbenchStageBar", app)
        self.assertIn("stageBarHtml", app)


class P2UsageSubtotalTests(_WorkspaceCase):
    """M1-5：会话用量小计 = 角色调度 / Manager 会话两小计（方案 6.5.1）。"""

    def setUp(self) -> None:
        super().setUp()
        self.cap_dir.joinpath("capsule.yaml").write_text(_capsule_yaml(
            "Tech-9", "某主题", {"owner": "wang.dandi"}), encoding="utf-8")
        self.proc, self.base, self.token = self.start_server(me="wang.dandi")

    def test_capsule_row_carries_role_and_manager_subtotals(self) -> None:
        """`usage.role` / `usage.manager` 两个小计与合计都在行上，缺用量单列。"""
        import yaml
        assignment = {
            "assignment_id": "Designer-1", "role": "Designer", "status": "succeeded",
            "deliverable": "03_设计.md",
            "attempts": [{
                "preference_number": 1, "profile": "designer-1",
                "started_at": _at(0),
                "ended_at": _at(0, 11), "exit_code": 0,
                "usage": {"models": {"claude/opus": {"in": 100, "out": 20,
                                                     "cache_read": 5, "cache_write": 0}}},
            }, {
                "preference_number": 2, "profile": "designer-2",
                "started_at": _at(0, 12),
                "ended_at": _at(0, 13), "exit_code": 0,
                "usage": "unknown",
            }],
        }
        self.cap_dir.joinpath("capsule.yaml").write_text(_capsule_yaml(
            "Tech-9", "某主题", {"owner": "wang.dandi",
                                "roles_manifest": {"assignments": [assignment]}}),
            encoding="utf-8")
        sessions = self.root / ".entropaxis" / "data" / "workbench"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "sessions.jsonl").write_text(
            json.dumps({"session_id": "s1", "turn_seq": 1, "capsule_id": "Tech-9",
                        "ended_at": _at(0, 14),
                        "usage": {"models": {"claude/opus": {"in": 40, "out": 8,
                                                             "cache_read": 0,
                                                             "cache_write": 0}}}},
                       ensure_ascii=False) + "\n", encoding="utf-8")
        status, payload = self._get(self.base + "/api/capsules", self.token)
        self.assertEqual(status, 200, payload)
        usage = next(r for r in payload if r["id"] == "Tech-9")["usage"]
        self.assertEqual(usage["role"]["in"], 100)
        self.assertEqual(usage["manager"]["in"], 40)
        self.assertEqual(usage["total"]["in"], 140)
        self.assertEqual(usage["unknown_count"], 1)

    def test_manager_session_dedup_by_session_and_turn(self) -> None:
        """同一 `(session_id, turn_seq)` 重复两行只计一次（6.4）。"""
        assignment = {
            "assignment_id": "Designer-1", "role": "Designer", "status": "succeeded",
            "deliverable": "03_设计.md",
            "attempts": [{
                "preference_number": 1, "profile": "designer-1",
                "started_at": _at(0),
                "ended_at": _at(0, 11), "exit_code": 0,
                "usage": {"models": {"claude/opus": {"in": 10, "out": 2,
                                                     "cache_read": 0, "cache_write": 0}}},
            }],
        }
        self.cap_dir.joinpath("capsule.yaml").write_text(_capsule_yaml(
            "Tech-9", "某主题", {"owner": "wang.dandi",
                                "roles_manifest": {"assignments": [assignment]}}),
            encoding="utf-8")
        sessions = self.root / ".entropaxis" / "data" / "workbench"
        sessions.mkdir(parents=True, exist_ok=True)
        turn = {"session_id": "s1", "turn_seq": 1, "capsule_id": "Tech-9",
                "ended_at": _at(0, 14),
                "usage": {"models": {"claude/opus": {"in": 40, "out": 8,
                                                     "cache_read": 0, "cache_write": 0}}}}
        (sessions / "sessions.jsonl").write_text(
            json.dumps(turn, ensure_ascii=False) + "\n"
            + json.dumps({**turn, "ended_at": _at(0, 14)},
                         ensure_ascii=False) + "\n",
            encoding="utf-8")
        status, payload = self._get(self.base + "/api/capsules", self.token)
        self.assertEqual(status, 200, payload)
        usage = next(r for r in payload if r["id"] == "Tech-9")["usage"]
        self.assertEqual(usage["manager"]["in"], 40)
        self.assertEqual(usage["total"]["in"], 50)

    def test_app_js_p2_renders_both_subtotals(self) -> None:
        """前端 P2 同时渲染两个小计与合计，不只显示一个总数。"""
        app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("角色调度", app)
        self.assertIn("Manager 会话", app)
        self.assertIn("合计", app)
        self.assertIn("u.role", app)
        self.assertIn("u.manager", app)
        self.assertIn("u.total", app)
        self.assertIn("unknown_count", app)


class P2ComposerTests(unittest.TestCase):
    """M1-5：输入区三态选择器（Q-8 / D-13）与附件入口。"""

    def test_exec_selector_has_three_states_and_server_disabled(self) -> None:
        """三态「按角色默认 / 本机 / 服务端」；M4 前服务端置灰。"""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        seg = html.split('id="execSeg"', 1)[1].split("</span>", 1)[0]
        self.assertIn("按角色默认", seg)
        self.assertIn("本机", seg)
        self.assertIn("服务端", seg)
        server = seg.split('data-v="server"', 1)[1].split(">", 1)[0]
        self.assertIn("disabled", server)

    def test_app_js_binds_exec_selector_and_binding_dropdown(self) -> None:
        """执行端选择与胶囊绑定下拉都由前端接线（只影响本会话之后的调度）。"""
        app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("execSeg", app)
        self.assertIn("bindCap", app)
        self.assertIn("execHint", app)
        self.assertIn("显式选择，覆盖 P8", app)

    def test_app_js_shows_unbound_capsule_label(self) -> None:
        """AC-12：run 文件 `capsule=null` 时徽标显示「未绑定胶囊」。"""
        app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("未绑定胶囊", app)


if __name__ == "__main__":
    unittest.main()
