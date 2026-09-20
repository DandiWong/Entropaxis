"""find_capsule 的检索收敛、结果过滤与降级留痕契约。"""
import sys
import tempfile
import unittest
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

import find_capsule as FC  # noqa: E402

REGISTRY = """\
# 工作区项目注册表

## 项目映射表

| 项目 ID | 名称 | 主目录 | 父项目 | 口语别名 | 外部看板映射 (Key-Value) | 备注 |
|---|---|---|---|---|---|---|
| （待填写） | （项目全称） | （主目录路径） | | | 未关联 | |
| alpha | 甲项目 | `A项目/` | | 甲, Alpha | 未关联 | |
| alpha-sub | 甲子项目 | `A项目/子线/` | alpha | 子线 | 未关联 | |
| beta | 乙项目 | `B项目/` | | 乙 | 未关联 | |

## 排除规则

- `Archive/`
"""


class RegistryParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        registry = self.root / ".entropaxis" / "data" / "templates"
        registry.mkdir(parents=True)
        (registry / "registry.md").write_text(REGISTRY, encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def test_placeholder_row_is_skipped(self):
        """占位行不能被当成真项目，否则空表工作区会解析出一个假项目。"""
        ids = [p["id"] for p in FC.load_registry(self.root)]
        self.assertEqual(ids, ["alpha", "alpha-sub", "beta"])

    def test_parent_column_is_read_not_inferred(self):
        """层级取自「父项目」列；平铺布局下路径前缀推不出父子关系。"""
        projects = {p["id"]: p for p in FC.load_registry(self.root)}
        self.assertEqual(projects["alpha-sub"]["parent"], "alpha")
        self.assertEqual(projects["alpha"]["parent"], "")

    def test_missing_registry_degrades_to_empty(self):
        """注册表缺失是合法初始态，不得抛异常阻断检索。"""
        self.assertEqual(FC.load_registry(Path(self.tmp.name) / "nowhere"), [])


class ScopeResolutionTests(unittest.TestCase):
    def setUp(self):
        self.projects = [
            {"id": "alpha", "dir": "A项目", "parent": "", "aliases": ["甲", "Alpha"]},
            {"id": "beta", "dir": "B项目", "parent": "", "aliases": ["乙"]},
        ]

    def test_exact_alias_wins(self):
        self.assertEqual(FC.resolve_scope("Alpha", self.projects)["id"], "alpha")

    def test_alias_match_is_case_insensitive(self):
        self.assertEqual(FC.resolve_scope("alpha", self.projects)["id"], "alpha")

    def test_unknown_query_keeps_full_scope(self):
        self.assertIsNone(FC.resolve_scope("无关词", self.projects))


class ResultShapingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for rel in ("20260101_旧胶囊", "20260930_新胶囊", "普通目录", "深层/20260615_中间胶囊"):
            (self.root / rel).mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def _run(self, query, **kw):
        return FC.find_capsule(query, self.root, **kw)

    def test_non_capsule_dirs_are_filtered_out(self):
        self.assertEqual(self._run("普通目录")["results"], [])

    def test_any_flag_returns_non_capsule_dirs(self):
        self.assertIn("普通目录", self._run("普通目录", capsules_only=False)["results"])

    def test_newest_capsule_first_by_basename_not_path(self):
        """日期在目录名开头，按整条路径排会先比父目录名，日期就失效了。"""
        names = [Path(p).name for p in self._run("胶囊")["results"]]
        self.assertEqual(names, ["20260930_新胶囊", "20260615_中间胶囊", "20260101_旧胶囊"])

    def test_results_are_workspace_relative(self):
        for item in self._run("胶囊")["results"]:
            self.assertFalse(Path(item).is_absolute())

    def test_empty_query_is_rejected(self):
        with self.assertRaises(FC.CapsuleFindError):
            self._run("   ")


class BackendDispatchTests(unittest.TestCase):
    def test_debian_fdfind_name_is_detected(self):
        """Debian 把 fd 装成 fdfind，只探测 fd 会在整个平台上静默降级。"""
        original = FC.shutil.which
        FC.shutil.which = lambda name: "/usr/bin/fdfind" if name == "fdfind" else None
        self.addCleanup(setattr, FC.shutil, "which", original)
        self.assertEqual(FC._fd_binary(), "fdfind")

    def test_missing_fd_reports_degradation_with_install_hint(self):
        """软降级必须留痕：降级到 find 时要说清原因和补救命令。"""
        original = FC.shutil.which
        FC.shutil.which = lambda name: None
        self.addCleanup(setattr, FC.shutil, "which", original)
        with tempfile.TemporaryDirectory() as tmp:
            _, backend, degraded = FC.search_workspace("x", Path(tmp), timeout=10)
        self.assertEqual(backend, "find")
        self.assertIn("fd", degraded)

    def test_doctor_reports_install_hint_for_missing_tool(self):
        original = FC.shutil.which
        FC.shutil.which = lambda name: None
        self.addCleanup(setattr, FC.shutil, "which", original)
        report = FC.doctor()
        self.assertIsNone(report["available"]["system_index"])
        if report["platform"] in FC.INSTALL_HINTS:
            self.assertIn("fd", report["install_hints"])


class LatestArtifactTests(unittest.TestCase):
    """受审对象定位（《治理指令》审计 §2 / 修正 §2）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        capsule = self.root / "20260901_某主题方案"
        capsule.mkdir()
        for name in ("01_调研.md", "02_方案.md", "05_审计报告.md", "04_Spec_Tech-1.md"):
            (capsule / name).write_text("x", encoding="utf-8")
        self.capsule = capsule

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_each_artifact_kind(self):
        for kind, expected in (("proposal", "02_方案.md"),
                               ("audit", "05_审计报告.md"),
                               ("spec", "04_Spec_Tech-1.md")):
            report = FC.find_artifacts(kind, self.root)
            self.assertIn(expected, [Path(r["path"]).name for r in report["results"]], kind)

    def test_results_ordered_newest_first(self):
        old = self.capsule / "02_方案.md"
        new_capsule = self.root / "20260920_新主题方案"
        new_capsule.mkdir()
        newer = new_capsule / "02_方案.md"
        newer.write_text("y", encoding="utf-8")
        import os
        os.utime(old, (1_600_000_000, 1_600_000_000))
        os.utime(newer, (1_700_000_000, 1_700_000_000))
        report = FC.find_artifacts("proposal", self.root)
        self.assertEqual(Path(report["results"][0]["path"]).parent.name, "20260920_新主题方案")

    def test_unknown_kind_reports_actionable_error(self):
        with self.assertRaises(FC.CapsuleFindError) as ctx:
            FC.find_artifacts("nope", self.root)
        self.assertIn("proposal", str(ctx.exception))

    def test_archive_and_vcs_dirs_are_skipped(self):
        for skipped in ("Archive", ".git"):
            buried = self.root / skipped / "20260101_旧方案"
            buried.mkdir(parents=True)
            (buried / "02_方案.md").write_text("x", encoding="utf-8")
        report = FC.find_artifacts("proposal", self.root)
        self.assertFalse([r for r in report["results"] if "Archive" in r["path"] or ".git" in r["path"]])

    def test_limit_truncates_and_flags(self):
        for day in range(2, 10):
            extra = self.root / f"202609{day:02d}_批量方案"
            extra.mkdir()
            (extra / "02_方案.md").write_text("x", encoding="utf-8")
        report = FC.find_artifacts("proposal", self.root, limit=3)
        self.assertEqual(len(report["results"]), 3)
        self.assertTrue(report["truncated"])
        self.assertGreater(report["count"], 3)

    def test_empty_result_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as empty:
            report = FC.find_artifacts("audit", Path(empty))
            self.assertEqual(report["results"], [])
            self.assertFalse(report["truncated"])


if __name__ == "__main__":
    unittest.main()
