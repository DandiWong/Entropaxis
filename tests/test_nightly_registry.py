"""nightly_registry.py：作业注册表的登记、唯一性、状态探测与异常分支。

实例数据落在 data/templates/nightly.yaml（merge-only），测试全部在临时工作区里跑，
不碰真实实例文件；launchd 状态探测用假 HOME 隔离，不断言本机真实 job。
"""
import io
from contextlib import redirect_stdout
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT))
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

import nightly_registry as nr  # noqa: E402


def _ns(**kw):
    base = dict(id=None, project=None, label=None, hour=2, minute=0, weekday=None,
                carrier="launchd", repo=None, commands=None, report_dir=None,
                note=None, disabled=False, enable=True, root=None)
    base.update(kw)
    return nr.argparse.Namespace(**base)


class NightlyRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_nightly_")
        self.root = Path(self._tmp.name)
        # 搭一个最小工作区：控制面骨架 + registry 实例
        (self.root / ".entropaxis/data/templates").mkdir(parents=True)
        shutil.copy(SYSTEM_ROOT / "templates/instance/nightly.template.yaml",
                    self.root / ".entropaxis/data/templates/nightly.yaml")
        shutil.copy(SYSTEM_ROOT / "templates/instance/registry.template.yaml",
                    self.root / ".entropaxis/data/templates/registry.yaml")
        reg = self.root / ".entropaxis/data/templates/registry.yaml"
        reg.write_text(reg.read_text(encoding="utf-8") + (
            "  - id: proj-a\n    name: 项目甲\n    path: 04ProjA/\n"
            "    code: [04ProjA/03_工程研发/app-a]\n"
            "  - id: proj-b\n    name: 项目乙\n    path: 04ProjB/\n    aliases: [乙]\n"
        ), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _add(self, **kw):
        args = _ns(id=kw.pop("id", "a-e2e"),
                   project=kw.pop("project", "proj-a"),
                   label=kw.pop("label", "com.org.a.nightly"),
                   commands=kw.pop("commands", ["scripts/test.sh"]),
                   root=self.root, **kw)
        buf = io.StringIO()
        with redirect_stdout(buf):
            res = nr.add(self.root, args)
        self.assertEqual(res, 0)

    # ── 正常路径 ──────────────────────────────────────────────────────────
    def test_add_and_list(self):
        self._add()
        jobs = nr.load(self.root)["jobs"]
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual(j["id"], "a-e2e")
        self.assertEqual(j["repo"], "04ProjA/03_工程研发/app-a", "repo 缺省应取 registry code[0]")
        self.assertEqual(j["schedule"], {"hour": 2, "minute": 0})
        self.assertNotIn("enabled", j, "enabled 缺省 true，不应显式落盘")

    def test_add_explicit_repo_and_report_dir(self):
        self._add(repo="custom/path", report_dir="docs/reports/", note="备注", weekday=6)
        j = nr.load(self.root)["jobs"][0]
        self.assertEqual(j["repo"], "custom/path")
        self.assertEqual(j["report_dir"], "docs/reports/")
        self.assertEqual(j["note"], "备注")
        self.assertEqual(j["schedule"]["weekday"], 6)

    def test_add_disabled_registers_without_enabling(self):
        self._add(disabled=True)
        self.assertFalse(nr.load(self.root)["jobs"][0]["enabled"])

    def test_enable_disable_toggle(self):
        self._add()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(nr.set_enabled(self.root, _ns(id="a-e2e", enable=False)), 0)
        self.assertFalse(nr.load(self.root)["jobs"][0]["enabled"])
        with redirect_stdout(buf):
            self.assertEqual(nr.set_enabled(self.root, _ns(id="a-e2e", enable=True)), 0)
        self.assertTrue(nr.load(self.root)["jobs"][0]["enabled"])

    def test_remove_drops_entry(self):
        self._add()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(nr.remove(self.root, _ns(id="a-e2e")), 0)
        self.assertEqual(nr.load(self.root)["jobs"], [])

    def test_add_requires_repo_when_project_has_no_code(self):
        """proj-b 无 code：必须显式 --repo，工具不猜。"""
        with self.assertRaises(nr.NightlyError) as ctx:
            self._add(project="proj-b", id="b-bench", label="com.org.b.nightly",
                      commands=["scripts/bench.py"])
        self.assertIn("无法推断 repo", str(ctx.exception))
        # 显式给了就能过
        self._add(project="proj-b", id="b-bench", label="com.org.b.nightly",
                  commands=["scripts/bench.py"], repo="04ProjB/03_工程研发/app-b")
        self.assertEqual(nr.load(self.root)["jobs"][0]["repo"], "04ProjB/03_工程研发/app-b")

    # ── 唯一性与非法输入 ──────────────────────────────────────────────────
    def test_duplicate_id_rejected(self):
        self._add()
        with self.assertRaises(nr.NightlyError) as ctx:
            self._add(id="a-e2e", label="com.org.a2.nightly")
        self.assertIn("已存在", str(ctx.exception))

    def test_duplicate_label_rejected(self):
        self._add()
        with self.assertRaises(nr.NightlyError) as ctx:
            self._add(id="a-other", label="com.org.a.nightly")
        self.assertIn("占用", str(ctx.exception))

    def test_unknown_project_rejected(self):
        with self.assertRaises(nr.NightlyError) as ctx:
            self._add(project="ghost")
        self.assertIn("不在 registry", str(ctx.exception))

    def test_illegal_label_rejected(self):
        with self.assertRaises(nr.NightlyError):
            self._add(label="bad label!")

    def test_bad_schedule_rejected_by_schema(self):
        with self.assertRaises(nr.NightlyError):
            self._add(hour=99)

    def test_empty_commands_rejected_by_schema(self):
        with self.assertRaises(nr.NightlyError):
            self._add(commands=[])

    def test_remove_unknown_rejected(self):
        with self.assertRaises(nr.NightlyError) as ctx:
            nr.remove(self.root, _ns(id="ghost"))
        self.assertIn("不存在", str(ctx.exception))

    # ── 状态探测（隔离 launchd 与本机 HOME） ──────────────────────────────
    def test_status_reports_no_plist_when_not_installed(self):
        self._add()
        with mock.patch.object(nr, "PLIST_DIR", self.root / "LaunchAgents"), \
             mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "", "")
            rows = nr.status_rows(self.root)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["plist"])
        self.assertFalse(rows[0]["loaded"])
        self.assertTrue(rows[0]["enabled"])
        self.assertEqual(rows[0]["repo"], "04ProjA/03_工程研发/app-a")

    def test_status_detects_loaded_plist_and_latest_report(self):
        self._add()
        plist_dir = self.root / "LaunchAgents"
        plist_dir.mkdir()
        (plist_dir / "com.org.a.nightly.plist").write_text("<plist/>", encoding="utf-8")
        reports = self.root / "04ProjA/03_工程研发/app-a/docs/05_reports"
        reports.mkdir(parents=True)
        (reports / "20261012_nightly_proj-a.md").write_text("x", encoding="utf-8")
        with mock.patch.object(nr, "PLIST_DIR", plist_dir), \
             mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "state = running", "")
            rows = nr.status_rows(self.root)
        self.assertTrue(rows[0]["loaded"])
        self.assertEqual(rows[0]["latest_report"], "20261012_nightly_proj-a.md")

    def test_status_launchctl_failure_is_not_loaded(self):
        self._add()
        plist_dir = self.root / "LaunchAgents"
        plist_dir.mkdir()
        (plist_dir / "com.org.a.nightly.plist").write_text("<plist/>", encoding="utf-8")
        with mock.patch.object(nr, "PLIST_DIR", plist_dir), \
             mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 113, "", "Could not find service")
            rows = nr.status_rows(self.root)
        self.assertFalse(rows[0]["loaded"], "plist 在但 launchctl 报错不得算生效")

    # ── 实例数据契约 ─────────────────────────────────────────────────────
    def test_registry_file_keeps_provenance_markers(self):
        self._add()
        text = (self.root / ".entropaxis/data/templates/nightly.yaml").read_text(encoding="utf-8")
        self.assertIn("policy: merge-only", text, "写入不得冲掉来源标记")
        self.assertIn("schema: .entropaxis/schemas/nightly.schema.json", text)

    def test_missing_registry_raises_actionable_error(self):
        (self.root / ".entropaxis/data/templates/nightly.yaml").unlink()
        with self.assertRaises(nr.NightlyError) as ctx:
            nr.load(self.root)
        self.assertIn("bootstrap.py", str(ctx.exception))

    def test_cli_list_empty_is_silent(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with mock.patch.object(sys, "argv",
                               ["nightly_registry.py", "--root", str(self.root), "list"]), \
             redirect_stdout(buf):
            try:
                nr.main()
            except SystemExit:
                pass
        self.assertIn("无 nightly 作业", buf.getvalue())



if __name__ == "__main__":
    unittest.main()
