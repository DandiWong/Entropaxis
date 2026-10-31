"""Tech-175 M1 契约：本地 HTTP 服务 `tools/workbench.py` 的路由、令牌与工具转调。

依据：`02_方案.md` 6.1（本地服务层：标准库 http.server 绑回环地址 + 随机端口 + 启动令牌，
读接口转调只读工具，写接口只转调现有写工具）与 Spec 第一节 API 契约。

M1-1（只读路由）：每个路由返回 200 且内容与对应工具 `--json` 输出一致；无令牌请求 401；
路由枚举里没有任何写存储授权策略的接口（AC-18）。测试只起服务、不发外网请求、不落盘缓存。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

WORKBENCH = SYSTEM_ROOT / "tools" / "workbench.py"
TOKEN_HEADER = "X-Entropaxis-Token"
_TODAY = __import__("datetime").date.today().strftime("%Y%m%d")  # init_capsule 用当天日期命名


def _run(argv: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *argv], capture_output=True, text=True, timeout=timeout)


def _capsule_yaml(cid: str, topic: str) -> str:
    import yaml
    payload = {"id": cid, "topic": topic, "mode": "full", "lifecycle": "draft",
               "seq_next": 1, "task_segments": [], "worklogs": []}
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


class _WorkspaceCase(unittest.TestCase):
    """带临时工作区的测试基类：自带 .entropaxis 骨架与一份胶囊。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "ws"
        self.addCleanup(self._td.cleanup)
        (self.root / ".entropaxis" / "data" / "templates").mkdir(parents=True)
        (self.root / ".entropaxis" / "tools").mkdir(parents=True)
        (self.root / "项目" / "docs" / f"{_TODAY}_某主题").mkdir(parents=True)
        (self.root / "项目" / "docs" / f"{_TODAY}_某主题" / "capsule.yaml").write_text(
            _capsule_yaml("Tech-9", "某主题"), encoding="utf-8")

    def start_server(self) -> tuple[subprocess.Popen, str, str]:
        """启动 workbench 服务，返回 (进程, base_url, 令牌)。令牌写 stderr 首行。"""
        proc = subprocess.Popen(
            [sys.executable, str(WORKBENCH), "--port", "0", "--no-open", "--root", str(self.root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self._stop, proc)
        token = ""
        base = ""
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

    @staticmethod
    def _get(url: str, token: str | None = None) -> tuple[int, object, dict]:
        request = urllib.request.Request(url)
        if token is not None:
            request.add_header("X-Entropaxis-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8")), dict(resp.headers)
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")
            return err.code, body, dict(err.headers)


class ReadOnlyRouteTests(_WorkspaceCase):
    """M1-1：七条只读路由转调只读工具，200 且内容与 CLI `--json` 一致。"""

    def setUp(self) -> None:
        super().setUp()
        self.proc, self.base, self.token = self.start_server()

    def test_no_token_requests_are_rejected_401(self) -> None:
        """无令牌请求一律 401：本机服务不对同机其它进程开放。"""
        for path in ("/api/capsules", "/api/todos", "/api/worklog", "/api/resume",
                     "/api/find?q=" + urllib.parse.quote("某主题"), "/api/projects", "/api/runs"):
            status, body, _ = self._get(self.base + path)
            self.assertEqual(status, 401, f"{path} 无令牌应 401，实际 {status}: {body}")
        status, _, _ = self._get(self.base + "/api/capsules", token="wrong-token")
        self.assertEqual(status, 401)

    def test_capsules_route_matches_cli_json(self) -> None:
        status, payload, _ = self._get(self.base + "/api/capsules", self.token)
        self.assertEqual(status, 200, payload)
        cli = _run([str(SYSTEM_ROOT / "tools" / "aggregate_worklog.py"),
                    "--json", "--root", str(self.root), "capsule-index"])
        self.assertEqual(cli.returncode, 0, cli.stderr)
        rows = json.loads(cli.stdout)
        self.assertTrue(any(row["id"] == "Tech-9" for row in payload))
        # M1-4：路由在 capsule_index 行上叠加 `stage_bar` 与显示口径字段（project_label /
        # sync_state），其余字段逐项相等。
        view_fields = ("stage_bar", "project_label", "sync_state")
        self.assertEqual([{k: v for k, v in row.items() if k not in view_fields} for row in payload],
                         [{k: v for k, v in row.items() if k not in view_fields} for row in rows])
        for row in payload:
            self.assertIn("stage_bar", row)
            self.assertEqual([s["stage"] for s in row["stage_bar"]],
                             ["01_调研", "02_方案", "03_设计", "04_Spec", "05_审计", "07_验收", "08_汇报"])
            self.assertIn("project_label", row)
            self.assertIn("sync_state", row)

    def test_capsule_detail_route_returns_single_row(self) -> None:
        status, payload, _ = self._get(self.base + "/api/capsules/Tech-9", self.token)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["id"], "Tech-9")
        self.assertEqual(payload["topic"], "某主题")

    def test_todos_route_matches_cli_json(self) -> None:
        (self.root / "项目" / "docs" / "Tasks.md").write_text("## Active\n- 写 Spec\n", encoding="utf-8")
        status, payload, _ = self._get(self.base + "/api/todos", self.token)
        self.assertEqual(status, 200, payload)
        cli = _run([str(SYSTEM_ROOT / "tools" / "aggregate_worklog.py"),
                    "--json", "--root", str(self.root), "todo-index"])
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(payload, json.loads(cli.stdout))

    def test_worklog_route_matches_cli_json(self) -> None:
        status, payload, _ = self._get(self.base + "/api/worklog?since=2026-10-01", self.token)
        self.assertEqual(status, 200, payload)
        cli = _run([str(SYSTEM_ROOT / "tools" / "aggregate_worklog.py"),
                    "--json", "--since", "2026-10-01", "--root", str(self.root)])
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(payload, json.loads(cli.stdout))

    def test_resume_route_matches_cli_json(self) -> None:
        status, payload, _ = self._get(self.base + "/api/resume", self.token)
        self.assertEqual(status, 200, payload)
        cli = _run([str(SYSTEM_ROOT / "tools" / "resume_context.py"),
                    "--root", str(self.root), "--json"])
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(payload, json.loads(cli.stdout))

    def test_find_route_matches_cli_json(self) -> None:
        status, payload, _ = self._get(self.base + "/api/find?q=" + urllib.parse.quote("某主题"), self.token)
        self.assertEqual(status, 200, payload)
        cli = _run([str(SYSTEM_ROOT / "tools" / "find_capsule.py"),
                    "某主题", "--root", str(self.root), "--json"])
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(payload, json.loads(cli.stdout))

    def test_find_route_without_query_is_client_error(self) -> None:
        status, body, _ = self._get(self.base + "/api/find", self.token)
        self.assertIn(status, (400, 422))
        self.assertIn("q", str(body))

    def test_projects_route_lists_registry(self) -> None:
        registry = self.root / ".entropaxis" / "data" / "templates" / "registry.yaml"
        registry.write_text(
            "projects:\n"
            "  - id: proj-a\n"
            "    name: 项目甲\n"
            "    path: 项目/\n"
            "    remote: 'r1:proj-a/'\n",
            encoding="utf-8")
        status, payload, _ = self._get(self.base + "/api/projects", self.token)
        self.assertEqual(status, 200, payload)
        # M1-6：树底部固定追加只读虚拟节点「系统（_root）」（Q-12 / D-17），
        # 业务项目仍是 registry 原条目，只增不改。
        self.assertEqual([p["id"] for p in payload], ["proj-a", "_root"])
        self.assertEqual(payload[0]["remote"], "r1:proj-a/")
        self.assertTrue(payload[1]["is_system"])
        self.assertEqual(payload[1]["remote"], "")

    def test_runs_route_lists_run_files(self) -> None:
        run_dir = self.root / ".entropaxis" / "data" / "logs" / "run"
        run_dir.mkdir(parents=True)
        (run_dir / "abc.1.json").write_text(json.dumps({
            "dispatch_id": "abc.1", "pid": os.getpid(), "role": "Builder",
            "profile": "builder-3", "agent": "omp", "model": "m",
            "started_at": "2026-10-30T09:00:00+00:00",
        }), encoding="utf-8")
        status, payload, _ = self._get(self.base + "/api/runs", self.token)
        self.assertEqual(status, 200, payload)
        self.assertEqual([r["dispatch_id"] for r in payload], ["abc.1"])
        self.assertTrue(payload[0]["alive"])
        self.assertEqual(payload[0]["role"], "Builder")

    def test_unknown_route_returns_404(self) -> None:
        status, _, _ = self._get(self.base + "/api/nope", self.token)
        self.assertEqual(status, 404)

    def test_route_table_has_no_storage_authorization_write_api(self) -> None:
        """AC-18：路由枚举里没有任何写存储授权策略的接口（NG-10）。

        写路由只转调既有写工具；远端授权策略、租约签发、冲突裁决一律不在本地服务。
        以路由表为准做行为断言，不靠源码字面扫描（字面扫描会把说明文字误判为接口）。
        """
        import workbench
        for forbidden in ("/api/lease", "/api/sync", "/api/remote", "/api/authz",
                          "/api/policy", "/api/rbac", "/api/conflicts"):
            self.assertNotIn(forbidden, workbench.READ_ROUTES)
            self.assertNotIn(forbidden, workbench.WRITE_ROUTES)
        # 写路由只覆盖四类既有写工具入口，且都不是存储授权策略
        self.assertEqual(set(workbench.WRITE_ROUTES),
                         {"/api/capsules", "/api/segments/open", "/api/segments/end",
                          "/api/worklog", "/api/files/open"})
        self.assertNotIn("/api/sync", workbench.READ_ROUTES)
        status, body, _ = self._get(self.base + "/api/sync", self.token)
        self.assertEqual(status, 404, f"/api/sync 不应存在: {body}")
        status, body, _ = self._get(self.base + "/api/leases", self.token)
        self.assertEqual(status, 404, f"/api/leases 不应存在: {body}")


class WriteRouteTests(_WorkspaceCase):
    """M1-2：写路由只转调既有写工具，字节变化与 CLI 直调一致，失败原样透出。"""

    def setUp(self) -> None:
        super().setUp()
        self.proc, self.base, self.token = self.start_server()
        self.capsule = self.root / "项目" / "docs" / f"{_TODAY}_某主题"

    def _post(self, path: str, body: dict, token: str | None = "use") -> tuple[int, object]:
        url = self.base + path
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token == "use":
            headers[TOKEN_HEADER] = self.token
        elif token:
            headers[TOKEN_HEADER] = token
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    @staticmethod
    def _cli(argv: list[str]) -> subprocess.CompletedProcess:
        return _run(argv)

    def test_post_capsule_creates_capsule_identical_to_cli(self) -> None:
        """POST /api/capsules → init_capsule.py：新建胶囊与 CLI 直调一致。

        两侧用不同主题目录（init_capsule 按 `日期_主题` 建目录，同主题会撞 no-clobber）、
        同一 Task ID：除 `topic` 一行外其余字节必须相同——参数转调没有走样。
        """
        cli = self._cli([str(SYSTEM_ROOT / "tools" / "init_capsule.py"),
                         str(self.root / "项目" / "docs"), "CLI 主题", "--id", "Tech-901"])
        self.assertEqual(cli.returncode, 0, cli.stderr)
        via_cli = (self.root / "项目" / "docs" / f"{_TODAY}_CLI 主题" / "capsule.yaml").read_text(encoding="utf-8")

        status, payload = self._post("/api/capsules", {
            "target_dir": str(self.root / "项目" / "docs"), "topic": "HTTP 主题", "id": "Tech-901"})
        self.assertEqual(status, 200, payload)
        via_http = (self.root / "项目" / "docs" / f"{_TODAY}_HTTP 主题" / "capsule.yaml").read_text(encoding="utf-8")
        self.assertEqual(via_http.replace('topic: "HTTP 主题"', 'topic: "X"'),
                         via_cli.replace('topic: "CLI 主题"', 'topic: "X"'),
                         "同一组参数下 HTTP 路由与 CLI 直调生成的 capsule.yaml 必须一致（topic 行除外）")
        self.assertEqual(payload["exit_code"], 0)
        self.assertIn("初始化成功", payload["stdout"])

    def test_post_capsule_without_token_is_401(self) -> None:
        status, payload = self._post("/api/capsules", {"target_dir": str(self.root), "topic": "x"},
                                     token=None)
        self.assertEqual(status, 401)
        status, payload = self._post("/api/capsules", {"target_dir": str(self.root), "topic": "x"},
                                     token="wrong")
        self.assertEqual(status, 401)

    def test_post_capsule_missing_field_is_400(self) -> None:
        status, payload = self._post("/api/capsules", {"target_dir": str(self.root)})
        self.assertEqual(status, 400)
        self.assertIn("topic", str(payload))

    def test_post_capsule_failure_passes_through_exit_code_and_stderr(self) -> None:
        """失败时非零退出码与 stderr 原文透出，不吞异常、不伪装成功。"""
        status, payload = self._post("/api/capsules", {
            "target_dir": str(self.capsule.parent), "topic": "某主题", "id": "Tech-9"})
        self.assertEqual(status, 502)
        self.assertEqual(payload["exit_code"], 1)
        cli = self._cli([str(SYSTEM_ROOT / "tools" / "init_capsule.py"),
                         str(self.capsule.parent), "某主题", "--id", "Tech-9"])
        self.assertEqual(cli.returncode, 1)
        self.assertIn("目标胶囊已存在", str(payload))
        self.assertIn("目标胶囊已存在", cli.stderr)

    def test_post_segment_open_matches_cli_bytes(self) -> None:
        """POST /api/segments/open → update_capsule.py --start-task：与 CLI 逐字节一致。

        `_save_capsule_yaml` 会把 open Manager 段的 `lastActivityAt` 刷成写入时刻
        （方案 §3.2 活动生产者），墙钟必然不同；因此比对的是「段结构 + startAt + baseline」，
        而不是含 lastActivityAt 的全文字节。
        """
        fixed_at = "2026-10-30T10:00:00+08:00"
        blank = _capsule_yaml("Tech-9", "某主题").encode("utf-8")

        cli_capsule = self.root / "项目" / "docs" / "20261030_CLI段"
        cli_capsule.mkdir(parents=True)
        (cli_capsule / "capsule.yaml").write_bytes(blank)
        cli = self._cli([str(SYSTEM_ROOT / "tools" / "update_capsule.py"),
                         str(cli_capsule), "--start-task", "--name", "写实现",
                         "--stage", "04_Spec", "--at", fixed_at])
        self.assertEqual(cli.returncode, 0, cli.stderr)

        (self.capsule / "capsule.yaml").write_bytes(blank)
        status, payload = self._post("/api/segments/open", {
            "capsule": str(self.capsule), "name": "写实现", "stage": "04_Spec", "at": fixed_at})
        self.assertEqual(status, 200, payload)

        import yaml
        via_cli = yaml.safe_load((cli_capsule / "capsule.yaml").read_text(encoding="utf-8"))
        via_http = yaml.safe_load((self.capsule / "capsule.yaml").read_text(encoding="utf-8"))
        for data in (via_cli, via_http):
            for seg in data["task_segments"]:
                seg.pop("lastActivityAt", None)  # 写入时刻，墙钟相关
        self.assertEqual(via_http, via_cli,
                         "同一组参数下 HTTP 路由与 CLI 直调的 capsule.yaml 必须一致")

    def test_post_segment_open_failure_passes_through(self) -> None:
        """缺 --name 时 CLI 报错：退出码与 stderr 原文透出，服务不返回 200。"""
        status, payload = self._post("/api/segments/open", {"capsule": str(self.capsule)})
        self.assertEqual(status, 400)
        self.assertIn("name", str(payload))

        cli = self._cli([str(SYSTEM_ROOT / "tools" / "update_capsule.py"),
                         str(self.capsule), "--start-task"])
        self.assertNotEqual(cli.returncode, 0)
        self.assertIn("name", cli.stderr)

    def test_post_worklog_matches_cli_bytes(self) -> None:
        """POST /api/worklog → update_capsule.py --log-time：工时条目与 CLI 逐字节一致。"""
        status, payload = self._post("/api/worklog", {
            "capsule": str(self.capsule), "spent": "1.5", "stage": "04_Spec",
            "summary": "HTTP 登记", "operator": "tester", "date": "2026-10-30"})
        self.assertEqual(status, 200, payload)
        self.assertIn("HTTP 登记", (self.capsule / "capsule.yaml").read_text(encoding="utf-8"))

        cli_capsule = self.root / "项目" / "docs" / "20261030_CLI工时"
        cli_capsule.mkdir(parents=True)
        (cli_capsule / "capsule.yaml").write_bytes(
            _capsule_yaml("Tech-9", "某主题").encode("utf-8"))
        cli = self._cli([str(SYSTEM_ROOT / "tools" / "update_capsule.py"),
                         str(cli_capsule), "--log-time", "1.5", "--stage", "04_Spec",
                         "--summary", "HTTP 登记", "--operator", "tester", "--date", "2026-10-30"])
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual((self.capsule / "capsule.yaml").read_bytes(),
                         (cli_capsule / "capsule.yaml").read_bytes(),
                         "同一组参数下 HTTP 路由与 CLI 直调的 capsule.yaml 必须逐字节一致")

    def test_post_worklog_failure_passes_through(self) -> None:
        status, payload = self._post("/api/worklog", {
            "capsule": str(self.capsule), "spent": "not-a-duration"})
        self.assertEqual(status, 502)
        self.assertEqual(payload.get("exit_code"), 1)
        self.assertIn("时长格式不合法", str(payload))

    def test_post_segment_end_failure_without_open_segment(self) -> None:
        """没有 open 段时 --end-task 报错：退出码与原文透出，服务不返回 200。"""
        status, payload = self._post("/api/segments/end", {"capsule": str(self.capsule)})
        self.assertEqual(status, 502)
        self.assertEqual(payload.get("exit_code"), 1)
        self.assertIn("detail", payload)

    def test_post_files_open_missing_path_is_400(self) -> None:
        status, payload = self._post("/api/files/open", {})
        self.assertEqual(status, 400)
        self.assertIn("path", str(payload))

    def test_post_files_open_reports_exit_code(self) -> None:
        """open_file.py 对不存在的目标返回非零：服务原样透出，不伪装成功。"""
        status, payload = self._post("/api/files/open", {
            "path": str(self.root / "不存在.md")})
        self.assertEqual(status, 502, payload)
        self.assertEqual(payload["exit_code"], 1)
        self.assertIn("无法打开", str(payload))


if __name__ == "__main__":
    unittest.main()
