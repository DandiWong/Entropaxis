"""Tech-175 M1 契约：本地 HTTP 服务 `tools/workbench.py` 的路由、令牌与工具转调。

依据：`02_方案.md` 6.1（本地服务层：标准库 http.server 绑 127.0.0.1 + 随机端口 + 启动令牌，
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
        (self.root / "项目" / "docs" / "20261030_某主题").mkdir(parents=True)
        (self.root / "项目" / "docs" / "20261030_某主题" / "capsule.yaml").write_text(
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
        self.assertEqual(payload, json.loads(cli.stdout))
        self.assertTrue(any(row["id"] == "Tech-9" for row in payload))

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
        self.assertEqual([p["id"] for p in payload], ["proj-a"])
        self.assertEqual(payload[0]["remote"], "r1:proj-a/")

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


if __name__ == "__main__":
    unittest.main()
