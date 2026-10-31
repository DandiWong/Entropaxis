"""Tech-175 M1-8 契约：P8 本机承载 / P10 设置（含 `me` 身份）。

依据：`02_方案.md` 4.2 P8 元素清单与「修改 roles.yaml 只能由用户明确操作」交互约束、
4.2 P10 元素清单、6.4「本机身份」（`me` 键）、6.6 权限与身份；`rules/角色协作.md:48`
（配置锁定）；Spec 4.2 的 M1-8 行。

覆盖（Spec 4.2 M1-8 测试要点）：
- P8 偏好链只读展示；改 roles.yaml 的写入口不存在（无 POST 路由、无写工具转调）。
- P10 设置项齐全（身份 / 工作区根 / 偏好 / 同步 / 诊断 / 规则·工具·Skill 只读链接）。
- `me` 缺省时 P1 显示引导绑定，且不自动落盘（不写 workspace-config.yaml）。

测试只起服务、只读工作区文件，不写任何数据文件。
"""
import json
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
STATIC_DIR = SYSTEM_ROOT / "tools" / "workbench" / "static"


def _capsule_yaml(cid: str, topic: str, extra: dict | None = None) -> str:
    import yaml
    payload = {"id": cid, "topic": topic, "mode": "full", "lifecycle": "draft",
               "seq_next": 1, "task_segments": [], "worklogs": []}
    payload.update(extra or {})
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


class _Case(unittest.TestCase):
    """带临时工作区的 M1-8 测试基类。"""

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

    def start_server(self, me: str | None = None) -> None:
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


class ExecutorsRouteTests(_Case):
    """M1-8：P8 本机承载只读展示，无写入口。"""

    def setUp(self) -> None:
        super().setUp()
        self.start_server(me="wang.dandi")

    def test_roles_carry_preference_chain_and_hard_lock(self) -> None:
        """偏好链（序号·profile·agent/model·超时）与硬门禁锁来自 roles.yaml 只读展示。"""
        payload = self._get("/api/executors")
        roles = {row["role"]: row for row in payload["roles"]}
        self.assertTrue(payload["present"], payload)
        for role in ("Architecture", "Researcher", "Designer", "Builder",
                     "Reviewer", "Maintainer", "Reporter"):
            self.assertIn(role, roles)
        builder = roles["Builder"]
        self.assertTrue(builder["preferences"])
        first = builder["preferences"][0]
        self.assertEqual(first["number"], 1)
        for key in ("profile", "agent", "model", "timeout_s", "executable"):
            self.assertIn(key, first)
        # 硬门禁角色标锁（role_preferences.HARD_ROLES = Reviewer / Maintainer）
        self.assertEqual(roles["Reviewer"]["gate"], "hard")
        self.assertEqual(roles["Maintainer"]["gate"], "hard")
        self.assertEqual(roles["Builder"]["gate"], "soft")

    def test_default_executor_per_role_is_first_preference(self) -> None:
        """每角色默认执行端 = 第 1 偏好（P2 显式选择优先，Q-8）。"""
        payload = self._get("/api/executors")
        self.assertEqual(payload["executors"][0]["id"], "local")
        for row in payload["roles"]:
            self.assertEqual(row["default_preference"],
                             1 if row["preferences"] else None)

    def test_no_write_entry_point_for_roles_yaml(self) -> None:
        """AC-18 / 角色协作 §48：服务端无任何改 roles.yaml 的写路由或写工具转调。"""
        source = (SYSTEM_ROOT / "tools" / "workbench.py").read_text(encoding="utf-8")
        write_routes = source.split("WRITE_ROUTES = ", 1)[1].split("\n\n", 1)[0]
        for forbidden in ("roles", "executor", "settings", "preference"):
            self.assertNotIn(forbidden, write_routes)
        for argv in ("setup_agents.py", "save_config", "--set-role", "--set-model",
                     "--set-timeout"):
            self.assertNotIn(argv, source)
        # POST 到本页两个路由一律 404：只读路由不接受写方法
        for route in ("/api/executors", "/api/settings"):
            request = urllib.request.Request(self.base + route, data=b"{}",
                                             headers={TOKEN_HEADER: self.token,
                                                      "Content-Type": "application/json"},
                                             method="POST")
            try:
                with urllib.request.urlopen(request, timeout=30) as resp:
                    status = resp.status
            except urllib.error.HTTPError as err:
                status = err.code
            self.assertEqual(status, 404, route)

    def test_roles_yaml_change_hint_is_present(self) -> None:
        """只提示「修改 roles.yaml 会影响全部调度」，不提供编辑器入口。"""
        payload = self._get("/api/executors")
        self.assertFalse(payload["writable"])
        self.assertIn("roles.yaml", payload["writable_hint"])
        self.assertIn("全部调度", payload["writable_hint"])


class SettingsRouteTests(_Case):
    """M1-8：P10 设置项齐全（只读）。"""

    def setUp(self) -> None:
        super().setUp()
        (self.root / ".entropaxis" / "data" / "templates" / "stakeholders.yaml").write_text(
            "people:\n- {id: wang.dandi, name: 王丹迪, aliases: [dandi]}\n",
            encoding="utf-8")
        self.start_server(me="wang.dandi")

    def test_settings_items_complete(self) -> None:
        """身份 / 工作区根 / 偏好 / 同步 / 诊断 / 只读链接六类设置项齐全。"""
        payload = self._get("/api/settings")
        self.assertEqual(payload["me"], "wang.dandi")
        self.assertFalse(payload["me_missing"])
        self.assertEqual(payload["workspace_root"], str(self.root.resolve()))
        self.assertIn("languages", payload["preferences"])
        self.assertIn("themes", payload["preferences"])
        self.assertEqual(payload["preferences"]["themes"], ["dark", "light", "system"])
        self.assertIn("logs_dir", payload["diagnostics"])
        self.assertEqual(set(payload["links"]), {"rules", "tools", "skills"})
        self.assertIn("wang.dandi", [p["id"] for p in payload["people"]])

    def test_app_js_p8_p10_render_readonly_views(self) -> None:
        """前端 P8/P10 渲染只读视图，不含任何写 roles.yaml 的入口。"""
        app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("renderP8", app)
        self.assertIn("renderP10", app)
        self.assertIn("修改 roles.yaml 会影响全部调度", app)
        self.assertIn("/api/executors", app)
        self.assertIn("/api/settings", app)
        # 前端不出现任何 setup_agents 写子命令
        for forbidden in ("set-role", "set-model", "set-timeout", "set-preference",
                          "move-preference", "remove-preference"):
            self.assertNotIn(forbidden, app)


class MeBindingGuidanceTests(_Case):
    """M1-8：`me` 缺省时 P1 引导绑定，且不自动落盘。"""

    def setUp(self) -> None:
        super().setUp()
        (self.root / ".entropaxis" / "data" / "templates" / "stakeholders.yaml").write_text(
            "people:\n- {id: wang.dandi, name: 王丹迪, aliases: [dandi]}\n",
            encoding="utf-8")
        # 不写 workspace-config.yaml：本机身份缺省
        self.start_server(me=None)

    def test_me_missing_flags_guidance_without_writing_config(self) -> None:
        """`me` 缺省：`/api/settings` 给出引导标记；workspace-config.yaml 不被创建。"""
        payload = self._get("/api/settings")
        self.assertIsNone(payload["me"])
        self.assertTrue(payload["me_missing"])
        config = self.root / ".entropaxis" / "data" / "templates" / "workspace-config.yaml"
        self.assertFalse(config.exists(), "服务不得自动写 workspace-config.yaml")

    def test_capsules_not_filtered_when_me_absent(self) -> None:
        """`me` 缺省不过滤胶囊（M1-5 契约）：引导绑定不等于空表。"""
        status_payload = self._get("/api/capsules")
        self.assertEqual([row["id"] for row in status_payload], ["Tech-9"])

    def test_p1_shows_binding_guidance(self) -> None:
        """P1 前端在 `me` 缺省时显示绑定引导（不自动写入）。"""
        app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("绑定", app)
        self.assertIn("me_missing", app)


if __name__ == "__main__":
    unittest.main()
