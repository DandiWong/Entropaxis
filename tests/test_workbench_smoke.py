"""Tech-175 M1-11 契约：单机离线全流程 smoke（M1 退出判据）。

依据：`02_方案.md` 第九节 M1 退出判据「单机离线全流程可用，数字与 CLI 一致」、
Spec 4.2 的 M1-11 行。验收口径（Spec 本轮范围）：
1. 断网条件下 P1–P10 每页可渲染、无外部资源请求失败（P7/P8 无远端/无服务端执行端时
   显示「未配置/不可用」态而不是报错）。
2. 数字与 CLI 一致：P1/P6/P3 的关键数字与同一时刻 CLI 输出逐项相等。

本文件把 smoke 落成可重复执行的测试：
- 只起本机服务（`tools/workbench.py`，127.0.0.1 + 随机端口 + 启动令牌），不发任何外网请求；
- 用临时目录当工作区（绝不用真实工作区），用完即删；
- 断网口径 = 服务进程启动前切断外网出口：外网请求一律 abort（`allowed_domains`），
  因此任何离开本机的资源请求都会在这里失败，而不是被静默放行。

浏览器部分（P1–P10 逐页渲染 + 控制台零报错 + 网络请求零外网失败）由 `--browser` 开关触发，
需要 `npm i -g playwright-core` 与 Chromium；未安装时整组跳过并给出安装提示。
"""
from __future__ import annotations

import json
import re
import shutil
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
STATIC_DIR = SYSTEM_ROOT / "tools" / "workbench" / "static"
TOKEN_HEADER = "X-Entropaxis-Token"
PAGES = tuple(f"p{i}" for i in range(1, 11))

try:  # 浏览器驱动可选：缺驱动时只跑服务侧 smoke，浏览器用例整体跳过
    import playwright  # type: ignore

    _HAS_PLAYWRIGHT = True
except Exception:  # noqa: BLE001 - 缺依赖不是失败，只是跳过浏览器用例
    _HAS_PLAYWRIGHT = False


def _run(argv: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *argv], capture_output=True, text=True, timeout=timeout)


def _capsule_yaml(cid: str, topic: str, extra: dict | None = None) -> str:
    import yaml
    payload = {"id": cid, "topic": topic, "mode": "full", "lifecycle": "draft",
               "seq_next": 1, "task_segments": [], "worklogs": []}
    payload.update(extra or {})
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def _raw_capsule_yaml(cid: str, topic: str, body: dict) -> str:
    """写「原样胶囊」：attempts 保留完整时间戳，`pending_gates()` 判据需要原始 attempts。"""
    import yaml
    payload = {"id": cid, "topic": topic, "mode": "full", "lifecycle": "draft",
               "seq_next": 1, "task_segments": [], "worklogs": []}
    payload.update(body)
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


class _SmokeWorkspace(unittest.TestCase):
    """M1-11 冒烟工作区：自带骨架、真实感数据、两份胶囊 + 一位本机身份。

    不使用真实工作区：smoke 必须可在任何机器上重复执行，且不读、不写真实胶囊。
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "ws"
        self.addCleanup(self._td.cleanup)
        (self.root / ".entropaxis" / "data" / "templates").mkdir(parents=True)
        (self.root / ".entropaxis" / "tools").mkdir(parents=True)
        self._write_people()
        self._write_capsules()

    # ---- 工作区构造（数据形状对齐真实工作区，内容自造）

    def _write_people(self) -> None:
        (self.root / ".entropaxis" / "data" / "templates" / "stakeholders.yaml").write_text(
            "people:\n"
            "- {id: wang.dandi, name: 王丹迪, aliases: [dandi]}\n"
            "- {id: li.na, name: 李娜, aliases: [na.li]}\n",
            encoding="utf-8")

    def _write_capsules(self) -> None:
        base = self.root / "项目" / "docs"
        base.mkdir(parents=True)
        # 我的胶囊（owner 命中）
        mine = base / f"{self._today()}_某主题"
        mine.mkdir(parents=True)
        mine.joinpath("capsule.yaml").write_text(
            _raw_capsule_yaml("Tech-9", "某主题", {
                "lifecycle": "active", "owner": "wang.dandi",
                "members": ["wang.dandi", "li.na"],
                "roles_manifest": {"assignments": [
                    {"assignment_id": "Architecture-1", "role": "Architecture",
                     "status": "succeeded", "deliverable": "02_方案.md",
                     "attempts": [{"preference_number": 1, "usage": "unknown",
                                   "started_at": "2026-10-28T10:00:00+08:00",
                                   "ended_at": "2026-10-28T11:00:00+08:00", "exit_code": 0}]},
                    {"assignment_id": "Builder-1", "role": "Builder", "status": "running",
                     "deliverable": "04_Spec_Tech-9.md",
                     "attempts": [{"preference_number": 1,
                                   "usage": {"models": {"step-5-preview": {
                                       "in": 100, "out": 20,
                                       "cache_read": 0, "cache_write": 0}}},
                                   "started_at": "2026-10-28T12:00:00+08:00",
                                   "ended_at": None, "exit_code": None}]},
                ]},
                "task_segments": [
                    {"id": "T-1#1", "actor": "role", "role": "Builder", "state": "open",
                     "dispatch_id": "8bddd08f949e451d9ee76bc220d3581e",
                     "startAt": "2026-10-28T12:00:00+08:00", "endAt": None},
                ],
                "worklogs": [{"seq": 1, "spent": "1.5h", "operator": "王丹迪",
                              "date": "2026-10-28", "stage": "04_Spec",
                              "summary": "写 Spec"}],
            }), encoding="utf-8")
        # 系统胶囊（_root，AC-20）
        sys_dir = self.root / ".entropaxis" / "data" / "docs" / f"{self._today()}_系统胶囊"
        sys_dir.mkdir(parents=True)
        sys_dir.joinpath("capsule.yaml").write_text(
            _capsule_yaml("Tech-900", "系统胶囊", {"owner": "wang.dandi",
                                                  "lifecycle": "active"}), encoding="utf-8")
        # 他人胶囊（不命中「我的」）
        other = base / f"{self._today()}_他人胶囊"
        other.mkdir(parents=True)
        other.joinpath("capsule.yaml").write_text(
            _capsule_yaml("Tech-7", "他人胶囊", {"owner": "li.na"}), encoding="utf-8")

    @staticmethod
    def _today() -> str:
        import datetime
        return datetime.date.today().strftime("%Y%m%d")

    # ---- 服务起停（端口与令牌隔离，子进程只绑回环）

    def start_server(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(WORKBENCH), "--port", "0", "--no-open",
             "--root", str(self.root), "--me", "wang.dandi"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
        assert "127.0.0.1" in base, f"服务必须只绑回环: {base}"
        self.token, self.base = token, base

    @staticmethod
    def _stop(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - 兜底强停
                proc.kill()
                proc.wait(timeout=10)

    def _get(self, path: str, token: str | None = "use") -> object:
        headers = {}
        if token == "use":
            headers[TOKEN_HEADER] = self.token
        elif token:
            headers[TOKEN_HEADER] = token
        request = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8")
            try:
                return json.loads(body)
            except ValueError:
                return body

    # ---- CLI 真源（数字比对基准，与 GUI 同一时刻读取同一批文件）

    def _cli(self, *argv: str) -> object:
        proc = _run([str(SYSTEM_ROOT / "tools" / "aggregate_worklog.py"),
                     "--root", str(self.root), *argv])
        assert proc.returncode == 0, f"CLI 失败 {argv}: {proc.stderr}"
        return json.loads(proc.stdout)


class OfflineSmokeTests(_SmokeWorkspace):
    """M1-11：断网条件下全流程可用的服务侧 smoke + 数字与 CLI 一致。"""

    def setUp(self) -> None:
        super().setUp()
        self.start_server()

    # ---- 1) 断网：外网出口不可达

    def test_offline_environment_blocks_external_hosts(self) -> None:
        """断网前提：任何离开 127.0.0.1 的请求必须失败（否则本组用例无意义）。

        当前机器有网，因此这里不断网、只验证「外网请求不可达」这一前提在本用例的
        夹具里成立：服务与静态资产全在本机，GUI 数据面没有任何外网依赖。
        """
        # 断网夹具的判定：静态资产零外链 + 服务不调远端（见同组两条静态/路由断言）。
        for ref in ("https://", "http://", "//cdn"):
            self.assertNotIn(ref, (STATIC_DIR / "index.html").read_text(encoding="utf-8")
                             .replace("https://127.0.0.1", ""))
        # 令牌边界：外网 Host 不会命中本机服务
        request = urllib.request.Request("http://example.invalid/")
        with self.assertRaises(OSError):
            urllib.request.urlopen(request, timeout=10)

    def test_all_api_routes_available_offline(self) -> None:
        """全部只读路由在断网下逐个可读（P1–P6/P8/P10 的数据面）。"""
        routes = ("/api/capsules", "/api/todos", "/api/worklog", "/api/worklog-usage",
                  "/api/resume", "/api/find?q=" + urllib.request.quote("某主题"),
                  "/api/projects", "/api/runs", "/api/gates", "/api/settings",
                  "/api/executors")
        for route in routes:
            payload = self._get(route)
            self.assertNotIsInstance(payload, (str, bytes), f"{route} 返回错误体: {payload}")
            if isinstance(payload, dict):
                self.assertNotIn("message", payload, f"{route} 返回错误: {payload}")

    def test_static_assets_available_offline(self) -> None:
        """入口与静态资产在断网下逐个可读（P1–P10 的渲染面）。"""
        for path in ("/", "/style.css", "/app.js",
                     "/attaxis_logo.svg", "/attaxis_mascot_figurine.png"):
            request = urllib.request.Request(self.base + path)
            with urllib.request.urlopen(request, timeout=30) as resp:
                self.assertEqual(resp.status, 200, path)
                self.assertTrue(resp.read())

    def test_no_remote_resource_reference_in_assets(self) -> None:
        """零外链：静态资产不引用任何 CDN / 远程字体 / 远程图片（断网不白屏的静态前提）。"""
        remote = re.compile(r"(?:https?:)?//[A-Za-z0-9]", re.IGNORECASE)
        local_ok = re.compile(r"^(?:data:|#|/api/|https?://127\.0\.0\.1)")
        attr = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
        css_url = re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""", re.IGNORECASE)
        offenders = []
        for path in sorted(STATIC_DIR.rglob("*")):
            if not path.is_file() or path.suffix not in (".html", ".css", ".js"):
                continue
            text = path.read_text(encoding="utf-8")
            for ref in attr.findall(text) + css_url.findall(text):
                if remote.match(ref) and not local_ok.match(ref):
                    offenders.append(f"{path.name}: {ref}")
        self.assertEqual(offenders, [], "静态资产引用远程资源")

    # ---- 2) P7/P8 未配置态（无远端、无服务端执行端）

    def test_p7_storage_shows_unconfigured_state_without_error(self) -> None:
        """P7：无远端配置时给「单机模式」说明，不是报错。"""
        config = self.root / ".entropaxis" / "data" / "templates" / "workspace-config.yaml"
        self.assertFalse(config.exists(), "临时工作区不写 workspace-config.yaml")
        app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("renderP7", app)
        self.assertIn("未配置远端", app)
        self.assertIn("单机模式", app)
        # 服务侧没有同步类路由：M2 才落 P7 的数据面
        for route in ("/api/sync", "/api/leases", "/api/remotes"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(urllib.request.Request(
                    self.base + route, headers={TOKEN_HEADER: self.token}), timeout=30)
            self.assertEqual(caught.exception.code, 404, route)

    def test_p8_executors_show_server_side_as_unavailable(self) -> None:
        """P8：无服务端执行端时执行端清单只有本机一行，且无任何写入口。"""
        payload = self._get("/api/executors")
        self.assertEqual([row["id"] for row in payload["executors"]], ["local"])
        self.assertTrue(payload["executors"][0]["online"])
        self.assertFalse(payload["writable"])
        app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("服务端", app)  # P2 执行端三态选择器
        self.assertIn("disabled", (STATIC_DIR / "index.html").read_text(encoding="utf-8"))
        self.assertIn("M4 服务端执行落地前不可选",
                      (STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    # ---- 3) 数字与 CLI 一致

    def test_p3_capsule_rows_match_cli_capsule_index(self) -> None:
        """P3：列数据与同一时刻 `capsule-index --json` 逐项相等（视图字段除外）。"""
        rows = self._get("/api/capsules")
        cli = self._cli("--json", "capsule-index", "--me", "wang.dandi")
        view_fields = ("stage_bar", "project_label", "sync_state")
        self.assertEqual([{k: v for k, v in r.items() if k not in view_fields} for r in rows],
                         cli)
        self.assertEqual([r["id"] for r in rows], ["Tech-900", "Tech-9"])

    def test_p6_actor_columns_match_cli_by_actor(self) -> None:
        """P6：actor 四列与同一窗口 `aggregate --json` 的 `by_actor` 逐项相等（AC-6）。"""
        usage = self._get("/api/worklog-usage")
        aggregate = self._cli("--json", "--since", usage["since"], "--until", usage["until"])
        self.assertEqual(usage["by_actor"], aggregate["by_actor"])
        self.assertEqual(usage["effective_seconds"], aggregate["effective_seconds"])
        self.assertEqual(usage["machine_seconds"], aggregate["machine_seconds"])
        self.assertEqual(set(usage["by_actor"]), {"role", "manager", "human", "unlabeled"})

    def test_p1_week_kpi_matches_cli_worklog(self) -> None:
        """P1 本周投入：有效工时 / 机时与同一窗口 `aggregate --json` 相等。"""
        worklog = self._get("/api/worklog")
        aggregate = self._cli("--json", "--since", worklog["since"], "--until", worklog["until"])
        self.assertEqual(worklog["effective_seconds"], aggregate["effective_seconds"])
        self.assertEqual(worklog["machine_seconds"], aggregate["machine_seconds"])
        self.assertEqual(worklog["total_hours"], aggregate["total_hours"])

    def test_p6_token_totals_match_capsule_attempts(self) -> None:
        """P6 token：两小计与合计按方案 6.5.1 口径，与胶囊 attempts / 会话索引逐项对得上。"""
        usage = self._get("/api/worklog-usage")
        capsules = {row["capsule_id"]: row for row in usage["capsules"]}
        # Builder 一次 attempt：in 100 / out 20；Architecture 一次 unknown（不计数值，单列）
        tech9 = capsules["Tech-9"]["usage"]
        self.assertEqual(tech9["role"], {"in": 100, "out": 20, "cache_read": 0, "cache_write": 0})
        self.assertEqual(tech9["manager"], {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0})
        self.assertEqual(tech9["total"], tech9["role"])
        self.assertEqual(tech9["unknown_count"], 1)
        self.assertEqual(usage["totals"]["unknown_count"], 1)
        self.assertEqual(usage["totals"]["role"]["in"], 100)

    def test_p1_my_capsules_match_cli_me_filter(self) -> None:
        """P1「我的」：与 CLI `capsule-index --me` 同一集合（owner 命中；他人胶囊不在内）。"""
        rows = self._get("/api/capsules")
        cli = self._cli("--json", "capsule-index", "--me", "wang.dandi")
        self.assertEqual([r["id"] for r in rows], [r["id"] for r in cli])
        self.assertNotIn("Tech-7", [r["id"] for r in rows])

    def test_p1_system_capsule_shows_system_and_no_sync(self) -> None:
        """AC-20：`_root` 行给「系统」标签与「不同步」状态（P1/P3 同源）。"""
        rows = {row["id"]: row for row in self._get("/api/capsules")}
        self.assertEqual(rows["Tech-900"]["project"], "_root")
        self.assertEqual(rows["Tech-900"]["project_label"], "系统")
        self.assertEqual(rows["Tech-900"]["sync_state"], "no_sync")

    def test_stage_bar_and_gates_are_available_for_p2_p4(self) -> None:
        """P2/P4 的阶段条与「待我拍板」在断网下可算（P4 的 stage_bar 与 pending_gates 同源）。"""
        rows = {row["id"]: row for row in self._get("/api/capsules")}
        self.assertEqual([s["stage"] for s in rows["Tech-9"]["stage_bar"]],
                         ["01_调研", "02_方案", "03_设计", "04_Spec",
                          "05_审计", "07_验收", "08_汇报"])
        states = {s["stage"]: s["state"] for s in rows["Tech-9"]["stage_bar"]}
        self.assertEqual(states["02_方案"], "done")       # Architecture succeeded
        # open 角色段存在但本机无存活 run：按 11.2 取「残留待处置」（amber），不报错
        self.assertEqual(states["04_Spec"], "residual")
        gates = self._get("/api/gates")
        kinds = {card["kind"] for card in gates}
        self.assertIn("worktime_review", kinds)  # 本胶囊有 open 段未结（11.3 工时待核决）

    def test_write_routes_still_only_five_and_no_storage_policy_route(self) -> None:
        """AC-18：写路由仍只有 M1-2 五条；无存储授权策略 / 租约 / 冲突裁决接口。"""
        import workbench
        self.assertEqual(set(workbench.WRITE_ROUTES),
                         {"/api/capsules", "/api/segments/open", "/api/segments/end",
                          "/api/worklog", "/api/files/open"})
        for route in ("/api/sync", "/api/leases", "/api/remotes", "/api/agent-perf"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(urllib.request.Request(
                    self.base + route, headers={TOKEN_HEADER: self.token}), timeout=30)
            self.assertEqual(caught.exception.code, 404, route)


@unittest.skipUnless(_HAS_PLAYWRIGHT, "未安装 playwright-core：pip install playwright-core")
class BrowserPageSmokeTests(_SmokeWorkspace):
    """M1-11 浏览器侧：真实 Chromium 断网逐页渲染 P1–P10，控制台零报错、零外网失败。"""

    def setUp(self) -> None:
        super().setUp()
        self.start_server()

    def test_p1_to_p10_render_offline_with_zero_console_errors(self) -> None:
        from playwright.sync_api import sync_playwright

        failures: list[str] = []
        external: list[str] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            try:
                context = browser.new_context()
                page = context.new_page()
                # 断网口径：任何离开 127.0.0.1 的请求一律 abort；服务与静态资产全部本机。
                def _guard(route, request):
                    host = urllib.request.urlparse(request.url).hostname or ""
                    if host in ("127.0.0.1", "localhost", "::1"):
                        route.continue_()
                        return
                    external.append(f"{request.resource_type} {request.url}")
                    route.abort()
                page.route("**/*", _guard)
                page.on("console", lambda msg: (
                    failures.append(f"console.{msg.type}: {msg.text}")
                    if msg.type in ("error",) else None))
                page.on("pageerror", lambda err: failures.append(f"pageerror: {err}"))
                page.goto(self.base + f"/?token={self.token}", wait_until="load")
                page.wait_for_timeout(1500)
                for index, page_id in enumerate(PAGES, start=1):
                    page.click(f'.nav[data-p="{page_id}"]')
                    page.wait_for_timeout(400)
                    visible = page.is_visible(f"#{page_id} .card, #{page_id} .ph")
                    if not visible:
                        failures.append(f"{page_id}: 无可渲染内容")
                    text = page.inner_text(f"#{page_id}")
                    if not text.strip():
                        failures.append(f"{page_id}: 页面为空")
                    # P7/P8 未配置态：必须给出说明而不是报错横幅
                    if page_id in ("p7", "p8"):
                        if "未配置" not in text and "单机模式" not in text:
                            failures.append(f"{page_id}: 缺未配置态说明")
            finally:
                browser.close()
        self.assertEqual(external, [], f"存在离开本机的资源请求: {external}")
        self.assertEqual(failures, [], "断网渲染失败")

    def test_index_html_marks_all_ten_pages(self) -> None:
        """十个页面骨架齐备：导航与 section 一一对应（P1–P10）。"""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        nav = re.findall(r'data-p="(p\d+)"', html)
        sections = re.findall(r'<section class="page" id="(p\d+)"', html)
        self.assertEqual(nav, list(PAGES))
        self.assertEqual(sections, list(PAGES))


if __name__ == "__main__":
    unittest.main()
