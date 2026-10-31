"""Tech-175 M1-7 契约：P6 工时与 Token（「我」视角，含 actor 四列）。

依据：`02_方案.md` 4.2 P6 元素清单、6.5.1 P6 token 口径（R4，PA-7）、6.3「我的」过滤；
Spec 4.2 的 M1-7 行与 4.3 AC 对应表（AC-6）。

覆盖（Spec 4.2 M1-7 测试要点）：
- fixture：角色 in 100 / Manager in 40 / unknown 1 次 → 显示 100 / 40 / 140、「缺用量 1 次」。
- 同一 `(session_id, turn_seq)` 重复两行只计一次。
- actor 四列之和等于 `by_actor`（AC-6）。
- 系统胶囊归 `_root`（AC-20）；时间维度用 `--since` 显式给周边界（M1-10）。

测试只起服务、只读工作区文件，不写任何数据文件。
"""
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

WORKBENCH = SYSTEM_ROOT / "tools" / "workbench.py"
AGGREGATE = SYSTEM_ROOT / "tools" / "aggregate_worklog.py"
TOKEN_HEADER = "X-Entropaxis-Token"
_TODAY = __import__("datetime").date.today().strftime("%Y%m%d")


def _run(argv: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *argv], capture_output=True, text=True, timeout=timeout)


def _capsule_yaml(cid: str, topic: str, extra: dict | None = None) -> str:
    import yaml
    payload = {"id": cid, "topic": topic, "mode": "full", "lifecycle": "draft",
               "seq_next": 1, "task_segments": [], "worklogs": []}
    payload.update(extra or {})
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def _attempt(started: str, usage: object, preference: int = 1) -> dict:
    return {"preference_number": preference, "profile": f"builder-{preference}",
            "started_at": started, "ended_at": "2026-11-01T11:00:00+08:00",
            "exit_code": 0, "usage": usage}


def _sessions_line(session_id: str, turn_seq: int, ended_at: str, usage: object) -> str:
    return json.dumps({"session_id": session_id, "turn_seq": turn_seq, "capsule_id": "Tech-9",
                       "ended_at": ended_at, "usage": usage}, ensure_ascii=False)


class _P6Case(unittest.TestCase):
    """带临时工作区的 P6 测试基类。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "ws"
        self.addCleanup(self._td.cleanup)
        (self.root / ".entropaxis" / "data" / "templates").mkdir(parents=True)
        (self.root / ".entropaxis" / "tools").mkdir(parents=True)
        self.cap_dir = self.root / "项目" / "docs" / f"{_TODAY}_某主题"
        self.cap_dir.mkdir(parents=True)
        self.sessions = self.root / ".entropaxis" / "data" / "workbench"
        self.sessions.mkdir(parents=True)

    def start_server(self, me: str | None = "wang.dandi") -> None:
        argv = [sys.executable, str(WORKBENCH), "--port", "0", "--no-open",
                "--root", str(self.root)]
        if me:
            argv += ["--me", me]
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop, self.proc)
        token = base = ""
        for line in self.proc.stderr:
            line = line.rstrip("\n")
            if line.startswith("token "):
                token = line.split(" ", 1)[1]
            if line.startswith("listening "):
                base = line.split(" ", 1)[1]
                break
        assert token and base, f"服务未就绪: token={token!r} base={base!r}"
        self.base, self.token = base, token

    @staticmethod
    def _stop(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - 兜底强停
                proc.kill()
                proc.wait(timeout=10)

    def _get(self, path: str) -> object:
        request = urllib.request.Request(self.base + path,
                                         headers={TOKEN_HEADER: self.token})
        with urllib.request.urlopen(request, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))


class P6TokenSubtotalTests(_P6Case):
    """M1-7：两小计 / 合计 / 缺用量单列（方案 6.5.1 样本）。"""

    def _write_capsule(self, attempts: list[dict], extra: dict | None = None) -> None:
        assignment = {"assignment_id": "Builder-1", "role": "Builder", "status": "succeeded",
                      "deliverable": "04_Spec_Tech-9.md", "attempts": attempts}
        self.cap_dir.joinpath("capsule.yaml").write_text(
            _capsule_yaml("Tech-9", "某主题",
                          {"owner": "wang.dandi", "roles_manifest": {"assignments": [assignment]},
                           **(extra or {})}), encoding="utf-8")

    def test_role_manager_total_and_unknown_count(self) -> None:
        """样本：角色 100 / Manager 40 / unknown 1 → 100、40、140、「缺用量 1 次」。"""
        self._write_capsule([
            _attempt("2026-11-01T10:00:00+08:00",
                     {"models": {"omp/step": {"in": 100, "out": 20, "cache_read": 5,
                                              "cache_write": 0}}}),
            _attempt("2026-11-01T12:00:00+08:00", "unknown", preference=2),
        ])
        (self.sessions / "sessions.jsonl").write_text(
            _sessions_line("s1", 1, "2026-11-01T14:00:00+08:00",
                           {"models": {"omp/step": {"in": 40, "out": 8, "cache_read": 0,
                                                    "cache_write": 0}}}) + "\n",
            encoding="utf-8")
        self.start_server()
        usage = self._get("/api/worklog-usage")
        rows = {row["capsule_id"]: row for row in usage["capsules"]}
        self.assertIn("Tech-9", rows)
        row = rows["Tech-9"]
        self.assertEqual(row["usage"]["role"]["in"], 100)
        self.assertEqual(row["usage"]["manager"]["in"], 40)
        self.assertEqual(row["usage"]["total"]["in"], 140)
        self.assertEqual(row["usage"]["unknown_count"], 1)
        self.assertEqual(usage["totals"]["role"]["in"], 100)
        self.assertEqual(usage["totals"]["manager"]["in"], 40)
        self.assertEqual(usage["totals"]["total"]["in"], 140)
        self.assertEqual(usage["totals"]["unknown_count"], 1)

    def test_duplicate_session_turn_counted_once(self) -> None:
        """同一 `(session_id, turn_seq)` 重复两行只计一次（6.4 / AC-6）。"""
        self._write_capsule([
            _attempt("2026-11-01T10:00:00+08:00",
                     {"models": {"omp/step": {"in": 10, "out": 2, "cache_read": 0,
                                              "cache_write": 0}}}),
        ])
        turn = _sessions_line("s1", 1, "2026-11-01T14:00:00+08:00",
                              {"models": {"omp/step": {"in": 40, "out": 8, "cache_read": 0,
                                                       "cache_write": 0}}})
        (self.sessions / "sessions.jsonl").write_text(
            turn + "\n" + turn + "\n", encoding="utf-8")
        self.start_server()
        usage = self._get("/api/worklog-usage")
        row = next(r for r in usage["capsules"] if r["capsule_id"] == "Tech-9")
        self.assertEqual(row["usage"]["manager"]["in"], 40)
        self.assertEqual(row["usage"]["total"]["in"], 50)

    def test_actor_four_columns_equal_by_actor(self) -> None:
        """AC-6：actor 四列与 `aggregate_worklog.py --json` 的 `by_actor` 逐项相等。"""
        self._write_capsule([
            _attempt("2026-11-01T10:00:00+08:00",
                     {"models": {"omp/step": {"in": 1, "out": 0, "cache_read": 0,
                                              "cache_write": 0}}}),
        ])
        # 一条 Manager 会话工时（worklog 带 actor=manager）+ 一条人工工时（无 actor）。
        self.cap_dir.joinpath("capsule.yaml").write_text(
            _capsule_yaml("Tech-9", "某主题", {
                "owner": "wang.dandi",
                "worklogs": [
                    {"date": "2026-11-01", "spent": 0.5, "stage": "04_Spec",
                     "summary": "Manager 会话", "operator": "Dandi Wong", "actor": "manager",
                     "seconds": 1800},
                    {"date": "2026-11-01", "spent": 0.25, "stage": "04_Spec",
                     "summary": "人工登记", "operator": "Dandi Wong", "seconds": 900},
                ]}), encoding="utf-8")
        self.start_server()
        usage = self._get("/api/worklog-usage")
        cli = _run([str(AGGREGATE), "--json", "--root", str(self.root),
                    "--since", usage["since"], "--until", usage["until"]])
        self.assertEqual(cli.returncode, 0, cli.stderr)
        aggregate = json.loads(cli.stdout)
        self.assertEqual(usage["by_actor"], aggregate["by_actor"])
        self.assertEqual(set(usage["by_actor"]), {"role", "manager", "human", "unlabeled"})
        self.assertEqual(usage["effective_seconds"], aggregate["effective_seconds"])
        self.assertEqual(usage["machine_seconds"], aggregate["machine_seconds"])

    def test_system_capsule_grouped_under_root(self) -> None:
        """AC-20：系统胶囊归 `_root`，只在本机视角显示。"""
        self._write_capsule([
            _attempt("2026-11-01T10:00:00+08:00",
                     {"models": {"omp/step": {"in": 100, "out": 0, "cache_read": 0,
                                              "cache_write": 0}}}),
        ])
        sys_dir = self.root / ".entropaxis" / "data" / "docs" / f"{_TODAY}_系统胶囊"
        sys_dir.mkdir(parents=True)
        sys_dir.joinpath("capsule.yaml").write_text(
            _capsule_yaml("Tech-900", "系统胶囊", {
                "owner": "wang.dandi",
                "roles_manifest": {"assignments": [{
                    "assignment_id": "Builder-1", "role": "Builder", "status": "succeeded",
                    "deliverable": "01_根系统治理.md",
                    "attempts": [_attempt("2026-11-01T10:00:00+08:00",
                                          {"models": {"omp/step": {"in": 7, "out": 0,
                                                                   "cache_read": 0,
                                                                   "cache_write": 0}}})]}]}}),
            encoding="utf-8")
        self.start_server()
        usage = self._get("/api/worklog-usage")
        rows = {row["capsule_id"]: row for row in usage["capsules"]}
        self.assertEqual(rows["Tech-900"]["project"], "_root")
        self.assertEqual(rows["Tech-900"]["project_label"], "系统")
        self.assertEqual(rows["Tech-900"]["sync_state"], "no_sync")
        self.assertEqual(rows["Tech-9"]["project"], "_unregistered")
        # 两胶囊都在「我」视角内：合计 = 100 + 7
        self.assertEqual(usage["totals"]["role"]["in"], 107)

    def test_window_bounds_use_since(self) -> None:
        """M1-10：窗口由 `--since` 显式给定，窗口外的 attempt 不计入。"""
        self._write_capsule([
            _attempt("2026-10-20T10:00:00+08:00",
                     {"models": {"omp/step": {"in": 999, "out": 0, "cache_read": 0,
                                              "cache_write": 0}}}),
            _attempt("2026-11-01T10:00:00+08:00",
                     {"models": {"omp/step": {"in": 100, "out": 0, "cache_read": 0,
                                              "cache_write": 0}}}),
        ])
        self.start_server()
        default = self._get("/api/worklog-usage")
        self.assertEqual(default["since"], __import__("workbench_view", fromlist=["x"])
                         .week_since())
        self.assertEqual(default["totals"]["role"]["in"], 100)
        wide = self._get("/api/worklog-usage?since=2026-10-01")
        self.assertEqual(wide["since"], "2026-10-01")
        self.assertEqual(wide["totals"]["role"]["in"], 1099)

    def test_usage_route_requires_token(self) -> None:
        """AC-18 / M1-1 边界：无令牌一律 401。"""
        self._write_capsule([])
        self.start_server()
        request = urllib.request.Request(self.base + "/api/worklog-usage")
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                status = resp.status
        except urllib.error.HTTPError as err:  # pragma: no cover - 期望路径
            status = err.code
        self.assertEqual(status, 401)

    def test_usage_route_is_read_only_get(self) -> None:
        """前端只读（硬约束）：该路由不提供 POST 写入口。"""
        self._write_capsule([])
        self.start_server()
        request = urllib.request.Request(self.base + "/api/worklog-usage", data=b"{}",
                                         headers={TOKEN_HEADER: self.token,
                                                  "Content-Type": "application/json"},
                                         method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                status = resp.status
        except urllib.error.HTTPError as err:
            status = err.code
        self.assertEqual(status, 404)

    def test_app_js_p6_renders_two_subtotals_and_actor_columns(self) -> None:
        """前端 P6 同时渲染两小计、合计、缺用量与 actor 四列（AC-6）。"""
        app = (SYSTEM_ROOT / "tools" / "workbench" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("角色调度", app)
        self.assertIn("Manager 会话", app)
        self.assertIn("缺用量", app)
        self.assertIn("by_actor", app)
        for column in ("role", "manager", "human", "unlabeled"):
            self.assertIn(column, app)


if __name__ == "__main__":
    unittest.main()
