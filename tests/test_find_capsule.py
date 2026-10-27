"""find_capsule 的检索收敛、结果过滤与降级留痕契约。"""
import sys
import tempfile
from unittest import mock
import unittest
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

import find_capsule as FC  # noqa: E402

REGISTRY = """\
exclude: [Archive/]
projects:
  - {id: alpha, name: 甲项目, path: A项目/, aliases: [甲, Alpha]}
  - {id: alpha-sub, name: 甲子项目, path: A项目/子线/, parent: alpha, aliases: [子线]}
  - {id: beta, name: 乙项目, path: B项目/, aliases: [乙]}
  - {name: 缺主键的残项}
"""


class RegistryParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        registry = self.root / ".entropaxis" / "data" / "templates"
        registry.mkdir(parents=True)
        (registry / "registry.yaml").write_text(REGISTRY, encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def test_placeholder_row_is_skipped(self):
        """缺 id/path 的残项不能被当成真项目。"""
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


class MachineBackendPlatformTests(unittest.TestCase):
    """机器范围后端按 platform.system() 分派：Darwin→mdfind、Windows→es、Linux→plocate，
    对应 sys.platform 的 darwin / win32 / linux；本类逐分支模拟三平台并验证降级留痕。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _patch_which(self, mapping):
        original = FC.shutil.which
        FC.shutil.which = lambda name: mapping.get(name)
        self.addCleanup(setattr, FC.shutil, "which", original)

    def _patch_run(self, hits):
        calls = []
        original = FC._run

        def fake_run(argv, timeout):
            calls.append(list(argv))
            return list(hits)

        FC._run = fake_run
        self.addCleanup(setattr, FC, "_run", original)
        return calls

    def test_darwin_uses_mdfind_and_reports_hidden_dir_caveat(self):
        with mock.patch.object(FC.platform, "system", return_value="Darwin"):
            self._patch_which({"mdfind": "/usr/bin/mdfind"})
            calls = self._patch_run([])
            _, backend, degraded = FC.search_machine("主题", Path(self.tmp.name), timeout=5)
        self.assertEqual(backend, "mdfind")
        self.assertEqual(calls[0][0], "mdfind")
        self.assertIn("Spotlight 不索引隐藏目录", degraded)

    def test_windows_uses_es_index_when_present(self):
        with mock.patch.object(FC.platform, "system", return_value="Windows"):
            self._patch_which({"es": r"C:\Tools\es.exe"})
            calls = self._patch_run([])
            _, backend, degraded = FC.search_machine("主题", Path(self.tmp.name), timeout=5)
        self.assertEqual(backend, "es")
        self.assertEqual(calls[0], ["es", "-p", "/ad", "主题"])
        self.assertIsNone(degraded)

    def test_linux_uses_plocate_and_keeps_dirs_only(self):
        capsule = Path(self.tmp.name) / "20260101_胶囊"
        capsule.mkdir()
        noise = Path(self.tmp.name) / "note.txt"
        noise.write_text("x", encoding="utf-8")
        with mock.patch.object(FC.platform, "system", return_value="Linux"):
            self._patch_which({"plocate": "/usr/bin/plocate"})
            self._patch_run([str(capsule), str(noise)])
            hits, backend, degraded = FC.search_machine("胶囊", Path(self.tmp.name), timeout=5)
        self.assertEqual(hits, [str(capsule)])  # locate 结果须过滤掉非目录命中
        self.assertEqual(backend, "plocate")
        self.assertIn("updatedb", degraded)

    def test_platform_without_index_degrades_to_realtime_walk(self):
        with mock.patch.object(FC.platform, "system", return_value="Windows"):
            self._patch_which({})  # 无 es、无 fd
            original_fd = FC._fd_binary
            FC._fd_binary = lambda: None
            self.addCleanup(setattr, FC, "_fd_binary", original_fd)
            _, backend, degraded = FC.search_machine("主题", Path(self.tmp.name), timeout=5)
        self.assertEqual(backend, "walk")
        self.assertIn("本平台无可用系统索引", degraded)
        self.assertIn("已降级为内置遍历", degraded)

        with mock.patch.object(FC.platform, "system", return_value="Linux"):
            self._patch_which({})  # 无 locate、无 fd
            original_fd = FC._fd_binary
            FC._fd_binary = lambda: None
            self.addCleanup(setattr, FC, "_fd_binary", original_fd)
            self._patch_run([])
            _, backend, degraded = FC.search_machine("主题", Path(self.tmp.name), timeout=5)
        self.assertEqual(backend, "find")
        self.assertIn("本平台无可用系统索引", degraded)
        self.assertIn("已降级为 find", degraded)

    def test_doctor_install_hints_follow_platform(self):
        for system, fragment in (("Darwin", "brew"), ("Linux", "apt"), ("Windows", "winget")):
            with self.subTest(system=system):
                with mock.patch.object(FC.platform, "system", return_value=system), \
                        mock.patch.object(FC.shutil, "which", lambda name: None):
                    report = FC.doctor()
                self.assertEqual(report["platform"], system)
                self.assertIn(fragment, report["install_hints"]["fd"])


if __name__ == "__main__":
    unittest.main()
