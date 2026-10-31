"""Tech-175 M1-6 契约：P3 胶囊列表 / P4 详情 / P5 项目页数据。

依据：`02_方案.md` 4.2 P3/P4/P5 行（列数据来自 `capsule_index`；解析失败行标红不中断整表；
P4 头部含状态机与阶段条；系统胶囊显示「系统（_root）」+ 灰色「不同步」；P5 按 parent 层级、
断链行标黄「目录不存在」）、Q-12 / D-17（系统节点只读）、AC-20。

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

LIFECYCLES = ("draft", "active", "delivered", "archived")


def _capsule_yaml(cid: str, topic: str, extra: dict | None = None) -> str:
    import yaml
    payload = {"id": cid, "topic": topic, "mode": "full", "lifecycle": "draft",
               "seq_next": 1, "task_segments": [], "worklogs": []}
    payload.update(extra or {})
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


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

    def _write_registry(self, text: str) -> None:
        (self.root / ".entropaxis" / "data" / "templates" / "registry.yaml").write_text(
            text, encoding="utf-8")


class CapsuleListTests(_WorkspaceCase):
    """M1-6 P3：列数据来自 `capsule_index`，解析失败行不中断整表。"""

    def setUp(self) -> None:
        super().setUp()
        self.proc, self.base, self.token = self.start_server()

    def test_list_rows_match_capsule_index_fields(self) -> None:
        """P3 十列所需的字段都在行上：id/topic/项目/生命周期/阶段/owner/工时/token/同步。"""
        status, payload = self._get(self.base + "/api/capsules", self.token)
        self.assertEqual(status, 200, payload)
        row = next(r for r in payload if r["id"] == "Tech-9")
        for key in ("id", "topic", "project", "lifecycle", "owner", "members",
                    "worklog_seconds", "usage", "stage_bar", "project_label", "sync_state"):
            self.assertIn(key, row)
        self.assertEqual(row["lifecycle"], "draft")

    def test_malformed_capsule_yaml_still_yields_row(self) -> None:
        """解析失败的行仍在表内（给路径与错误摘要），其余行正常渲染（聚合器坏数据容错）。"""
        bad_dir = self.root / "项目" / "docs" / f"{_TODAY}_坏胶囊"
        bad_dir.mkdir(parents=True)
        (bad_dir / "capsule.yaml").write_text(
            "id: Tech-8\ntopic: \"坏胶囊\"\nlifecycle: draft\nbad: [unclosed\n", encoding="utf-8")
        status, payload = self._get(self.base + "/api/capsules", self.token)
        self.assertEqual(status, 200, payload)
        ids = {r["id"] for r in payload}
        self.assertIn("Tech-9", ids)
        # 聚合层对坏 YAML 有正则后备：行仍在，且 stage_bar 不因坏数据抛错
        for row in payload:
            self.assertEqual(len(row["stage_bar"]), 7)

    def test_lifecycle_is_one_of_four_values(self) -> None:
        """生命周期取值收敛到四态（P3 筛选下拉口径）。"""
        for life in LIFECYCLES:
            d = self.root / "项目" / "docs" / f"{_TODAY}_{life}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "capsule.yaml").write_text(
                _capsule_yaml(f"Tech-{life}", life, {"lifecycle": life}), encoding="utf-8")
        status, payload = self._get(self.base + "/api/capsules", self.token)
        self.assertEqual(status, 200, payload)
        seen = {r["lifecycle"] for r in payload}
        self.assertTrue(seen.issubset(set(LIFECYCLES)))
        self.assertEqual(seen, set(LIFECYCLES))

    def test_system_capsule_row_shows_system_and_no_sync(self) -> None:
        """AC-20 / Q-12：P3 项目列「系统」、同步列灰色「不同步」。"""
        sys_dir = self.root / ".entropaxis" / "data" / "docs" / f"{_TODAY}_系统胶囊"
        sys_dir.mkdir(parents=True)
        (sys_dir / "capsule.yaml").write_text(
            _capsule_yaml("Tech-900", "系统胶囊"), encoding="utf-8")
        status, payload = self._get(self.base + "/api/capsules", self.token)
        self.assertEqual(status, 200, payload)
        row = next(r for r in payload if r["id"] == "Tech-900")
        self.assertEqual(row["project"], "_root")
        self.assertEqual(row["project_label"], "系统")
        self.assertEqual(row["sync_state"], "no_sync")


class CapsuleDetailTests(_WorkspaceCase):
    """M1-6 P4：单行详情含头部四要素 + 状态机四态 + 阶段条。"""

    def setUp(self) -> None:
        super().setUp()
        self.cap_dir.joinpath("capsule.yaml").write_text(_capsule_yaml(
            "Tech-9", "某主题", {"owner": "wang.dandi", "members": ["wang.dandi", "li.na"],
                                "review_due_at": "2026-11-10",
                                "closure_deadline": "2026-12-01"}), encoding="utf-8")
        self.proc, self.base, self.token = self.start_server(me="wang.dandi")

    def test_detail_route_returns_full_row(self) -> None:
        """P4 头部字段：主题、ID、项目、生命周期、owner/members、两个日期、阶段条。"""
        status, payload = self._get(self.base + "/api/capsules/Tech-9", self.token)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["id"], "Tech-9")
        self.assertEqual(payload["topic"], "某主题")
        self.assertEqual(payload["lifecycle"], "draft")
        self.assertEqual(payload["owner"], "wang.dandi")
        self.assertEqual(payload["members"], ["wang.dandi", "li.na"])
        self.assertEqual(payload["review_due_at"], "2026-11-10")
        self.assertEqual(payload["closure_deadline"], "2026-12-01")
        self.assertEqual(len(payload["stage_bar"]), 7)
        self.assertIn("project_label", payload)
        self.assertIn("sync_state", payload)

    def test_unknown_capsule_id_is_404(self) -> None:
        status, payload = self._get(self.base + "/api/capsules/Tech-404", self.token)
        self.assertEqual(status, 404)

    def test_detail_row_matches_list_row(self) -> None:
        """P4 与 P3 同一行同源：不另起一套读取路径。"""
        _, listed = self._get(self.base + "/api/capsules", self.token)
        _, detail = self._get(self.base + "/api/capsules/Tech-9", self.token)
        self.assertEqual(detail, next(r for r in listed if r["id"] == "Tech-9"))

    def test_app_js_renders_p4_header_state_machine(self) -> None:
        """前端 P4 渲染状态机四态与阶段条，不自己重算判据。"""
        app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        for life in LIFECYCLES:
            self.assertIn(life, app)
        self.assertIn("stageBarHtml", app)
        self.assertIn("lifecycle", app)


class ProjectTreeTests(_WorkspaceCase):
    """M1-6 P5：项目树按 parent 层级、断链标黄、系统节点只读。"""

    def setUp(self) -> None:
        super().setUp()
        self._write_registry(
            "projects:\n"
            "  - id: proj-a\n"
            "    name: 项目甲\n"
            "    path: 项目/\n"
            "  - id: proj-a-child\n"
            "    name: 子项目\n"
            "    path: 项目/子/\n"
            "    parent: proj-a\n"
            "  - id: proj-broken\n"
            "    name: 断链项目\n"
            "    path: 不存在目录/\n"
        )
        self.proc, self.base, self.token = self.start_server()

    def test_projects_route_exposes_parent_and_broken_flag(self) -> None:
        """P5 树需要的字段：id/name/path/parent、目录是否存在（断链判据）。"""
        status, payload = self._get(self.base + "/api/projects", self.token)
        self.assertEqual(status, 200, payload)
        rows = {p["id"]: p for p in payload}
        self.assertEqual(set(rows) - {"_root"}, {"proj-a", "proj-a-child", "proj-broken"})
        self.assertEqual(rows["proj-a-child"]["parent"], "proj-a")
        self.assertEqual(rows["proj-a"]["parent"], "")
        self.assertTrue(rows["proj-broken"]["dir_missing"])
        self.assertFalse(rows["proj-a"]["dir_missing"])

    def test_system_node_is_appended_as_readonly_root(self) -> None:
        """Q-12 / D-17：树底部固定只读虚拟节点「系统（_root）」。"""
        status, payload = self._get(self.base + "/api/projects", self.token)
        self.assertEqual(status, 200, payload)
        root = next(p for p in payload if p["id"] == "_root")
        self.assertTrue(root["is_system"])
        self.assertEqual(root["name"], "系统（_root）")
        self.assertEqual(root["remote"], "")

    def test_project_rows_carry_remote_binding(self) -> None:
        """远端绑定字段随行下发（P5 详情用；本轮无远端则为空串）。"""
        self._write_registry(
            "projects:\n"
            "  - id: proj-a\n"
            "    name: 项目甲\n"
            "    path: 项目/\n"
            "    remote: 'r1:proj-a/'\n"
        )
        status, payload = self._get(self.base + "/api/projects", self.token)
        self.assertEqual(status, 200, payload)
        row = next(p for p in payload if p["id"] == "proj-a")
        self.assertEqual(row["remote"], "r1:proj-a/")

    def test_app_js_renders_project_tree_with_broken_marker(self) -> None:
        """前端 P5 渲染树、断链标黄与系统只读节点（文案来自服务端行字段）。"""
        app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("ptree", app)
        self.assertIn("pdetail", app)
        self.assertIn("目录不存在", app)
        self.assertIn("is_system", app)
        self.assertIn("dir_missing", app)
        self.assertIn("在办", app)


if __name__ == "__main__":
    unittest.main()
