"""目录发现、确认登记和人工配置保护的回归测试。"""
import contextlib
import io
import tempfile
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import content_evidence, project_registry, scan_workspace
import zipfile
from unittest import mock


class ScanWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config = self.root / ".entropaxis/data/templates"
        config.mkdir(parents=True)
        self.reg = config / "registry.yaml"
        self.ws = config / "workspace-config.yaml"
        self.reg.write_text("# keep registry\nexclude: [ignored/] # manual\nprojects: []\n")
        self.ws.write_text("# keep workspace\nshared_dirs: []\norg: {full_name: Manual}\n")

    def folder(self, name, marker=None):
        p = self.root / name
        p.mkdir(parents=True, exist_ok=True)
        if marker:
            (p / marker).write_text("")
        return p

    def scan(self):
        return scan_workspace.scan_workspace(self.root)

    def test_candidates_and_ownership(self):
        self.folder("product", "AGENTS.md")
        self.folder("product/01_项目管理")
        self.folder("product/apps/api", "pyproject.toml")
        self.folder("product/00_知识库", "index.md")
        self.folder("domain-knowledge", "index.md")
        self.folder("archive")
        result = self.scan()
        p = result["projects"][0]
        self.assertEqual(p["path"], "product")
        self.assertEqual(p["code"], ["product/apps/api"])
        self.assertEqual(p["knowledge"], ["product/00_知识库"])
        self.assertEqual(result["shared_kbs"][0]["dir"], "domain-knowledge/")
        self.assertEqual(result["excludes"][0]["path"], "archive/")

    def test_content_evidence_makes_plain_directory_a_candidate(self):
        p = self.folder("business")
        for i in range(4):
            (p / f"report{i}.md").write_text("商业内容分析", encoding="utf-8")
        self.folder("assets-product", "package.json")
        self.folder("reporting", "pyproject.toml")
        self.folder("empty")
        result = self.scan()
        self.assertEqual({p["path"] for p in result["projects"]}, {"assets-product", "business", "reporting"})
        self.assertEqual([p["path"] for p in result["pending"]], ["empty"])
        evidence = next(p for p in result["projects"] if p["path"] == "business")["evidence"][0]
        self.assertIn("内容证据", evidence)

    def test_same_theme_directories_propose_merge_with_moves_draft(self):
        for name in ("专利资料", "商标资料"):
            directory = self.root / name
            directory.mkdir()
            (directory / "说明.md").write_text("专利申请流程与规范", encoding="utf-8")
        # 未登记且目标目录尚未建：只给建议名，不出 moves 草案。
        result = self.scan()
        self.assertEqual(len(result["merges"]), 1)
        merge = result["merges"][0]
        self.assertEqual(merge["members"], ["专利资料", "商标资料"])
        self.assertEqual(merge["moves"], [])
        self.assertIn("待人工命名", merge["reason"])
        # 人工命名并建好目录后重跑，才出可执行的 moves 草案。
        (self.root / "资料").mkdir()
        result = self.scan()
        merge = result["merges"][0]
        self.assertEqual(merge["moves"], ["专利资料/\t资料/专利资料/", "商标资料/\t资料/商标资料/"])

    def test_existing_excludes_shared_and_incremental_code(self):
        self.reg.write_text("exclude: [ignored/, '**/skip/']\nprojects:\n- id: product\n  path: product/\n  aliases: [handmade]\n  code: [product]\n")
        self.ws.write_text("shared_dirs: [{dir: library/, purpose: manual}]\n")
        self.folder("ignored", "package.json")
        self.folder("library", "package.json")
        self.folder("product", "package.json")
        self.folder("product/apps/api", "package.json")
        self.folder("product/apps/skip", "package.json")
        r = self.scan()
        self.assertFalse(r["projects"])
        self.assertFalse(r["shared_kbs"])
        self.assertEqual(r["updates"][0]["code"], ["product/apps/api"])

    def test_no_dependency_or_external_link_scan(self):
        self.folder("product", "package.json")
        self.folder("product/node_modules/vendor", "package.json")
        self.folder("product/dist/compiled", "package.json")
        outside = Path(self.tmp.name).parent
        (self.root / "linked").symlink_to(outside, target_is_directory=True)
        r = self.scan()
        self.assertEqual(r["projects"][0]["code"], ["product"])
        self.assertTrue(any(p["path"] == "linked" for p in r["pending"]))

    def test_preview_cli_never_writes(self):
        self.folder("app", "package.json")
        before = self.reg.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scan_workspace.main(["--root", str(self.root)]), 0)
        self.assertEqual(self.reg.read_bytes(), before)
        self.assertFalse(scan_workspace.register_scanned(self.root, self.scan()))

    # ---- M-2/M-4：证据边界与证据不足的可见性 ----

    def test_excluded_subtree_is_not_content_evidence(self):
        """审计 S-4 excluded 反例：唯一文件都在显式排除子树内，不得成为候选或归并证据。"""
        self.reg.write_text("exclude: [alpha/private/, beta/private/]\nprojects: []\n")
        for name in ("alpha", "beta"):
            private = self.root / name / "private"
            private.mkdir(parents=True)
            (private / "notes.txt").write_text("confidential merger evidence shared topic", encoding="utf-8")
        result = self.scan()
        self.assertFalse(result["projects"], "只剩排除材料的目录不得成为候选")
        self.assertFalse(result["merges"], "排除材料不得凑出归并建议")
        reasons = {p["path"]: p["reason"] for p in result["pending"]}
        self.assertEqual(set(reasons), {"alpha", "beta"})
        for reason in reasons.values():
            self.assertIn("证据不足", reason)
            self.assertIn("边界", reason)

    def test_broken_binary_insufficient_evidence_via_real_cli(self):
        """审计 S-4 broken 反例：损坏 DOCX 经真实 CLI 输出证据不足与文件身份，其余目录继续。"""
        for name in ("alpha", "beta"):
            directory = self.root / name
            directory.mkdir()
            (directory / "bad.docx").write_bytes(b"not zip")
        done = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "tools" / "scan_workspace.py"),
             "--root", str(self.root)],
            capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("projects=0", done.stdout)
        self.assertIn("merges=0", done.stdout)
        self.assertIn("内容证据不足", done.stdout)
        self.assertIn("bad.docx", done.stdout)
        self.assertEqual(done.stdout.count("内容证据不足"), 2, "两个损坏目录都要可见，不许静默丢弃")

    def test_structural_candidate_parse_failure_is_visible_via_real_cli(self):
        """R2-S4 反例：未登记结构候选（package.json）里的损坏 DOCX，解析失败不许静默丢弃。"""
        for name in ("alpha", "beta"):
            directory = self.root / name
            directory.mkdir()
            (directory / "package.json").write_text("{}")
            (directory / "bad.docx").write_bytes(b"not zip")
        done = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "tools" / "scan_workspace.py"),
             "--root", str(self.root)],
            capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("projects=2", done.stdout, "结构候选保持正常候选，不因解析失败中断")
        self.assertIn("bad.docx", done.stdout)
        self.assertEqual(done.stdout.count("内容证据不足"), 2, "两个结构候选的解析失败都要可见")

    def test_oversized_binary_records_insufficient_evidence(self):
        """审计 S-4 oversized 反例：超限文件记证据不足，不再进入候选或归并。"""
        directory = self.root / "alpha"
        directory.mkdir()
        with zipfile.ZipFile(directory / "notes.docx", "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("word/document.xml", "<w:t>" + "alpha evidence boundary " * 64 + "</w:t>")
        with patch.object(content_evidence, "MAX_BINARY_BYTES", 512):
            result = self.scan()
        self.assertFalse(result["projects"])
        self.assertFalse(result["merges"])
        reason = next(p["reason"] for p in result["pending"] if p["path"] == "alpha")
        self.assertIn("证据不足", reason)
        self.assertIn("notes.docx", reason)

    def test_registered_nested_material_not_shell_evidence(self):
        """M-2：已登记嵌套项目的材料不得充当其上层外壳目录的归并证据。"""
        self.reg.write_text(
            "projects:\n"
            "  - {id: jia, name: 项目甲, path: 项目甲/, aliases: []}\n"
            "  - {id: jia-lib, name: 共享库, path: 项目甲/共享库/, aliases: []}\n"
            "  - {id: yi, name: 项目乙, path: 项目乙/, aliases: []}\n")
        shared_lib = self.root / "项目甲" / "共享库"
        shared_lib.mkdir(parents=True)
        (shared_lib / "说明.md").write_text("专利申请流程与规范", encoding="utf-8")
        other = self.root / "项目乙"
        other.mkdir()
        (other / "说明.md").write_text("专利申请流程与规范", encoding="utf-8")
        result = self.scan()
        self.assertFalse(result["merges"], "归属于嵌套项目的材料不得让外壳目录与别人聚簇")

    def test_apply_requires_selection(self):
        self.folder("app", "package.json")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(scan_workspace.main(["--root", str(self.root), "--apply"]), 0)

    def test_confirmed_registration_preserves_comments_and_is_idempotent(self):
        self.folder("app", "package.json")
        self.folder("knowledge", "index.md")
        args = ["--root", str(self.root), "--apply", "--select", "app", "--select", "knowledge"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scan_workspace.main(args), 0)
            before = (self.reg.read_bytes(), self.ws.read_bytes())
            self.assertEqual(scan_workspace.main(args), 0)
        self.assertEqual((self.reg.read_bytes(), self.ws.read_bytes()), before)
        self.assertIn("# keep registry", self.reg.read_text())
        self.assertIn("# manual", self.reg.read_text())
        self.assertIn("# keep workspace", self.ws.read_text())
        self.assertIn("org: {full_name: Manual}", self.ws.read_text())
        self.assertEqual(len(project_registry.load_projects(self.root)), 1)

    def test_selected_update_keeps_existing_fields(self):
        self.reg.write_text("exclude: []\nprojects:\n  - id: app\n    path: app/\n    aliases: [custom] # retain\n    code: [app]\n    note: human\n")
        self.folder("app", "package.json")
        self.folder("app/apps/api", "package.json")
        scan_workspace.register_scanned(self.root, self.scan(), apply=True)
        p = project_registry.load_projects(self.root)[0]
        self.assertEqual(p["aliases"], ["custom"])
        self.assertEqual(p["note"], "human")
        self.assertEqual(p["code"], ["app", "app/apps/api"])
        self.assertIn("# retain", self.reg.read_text())

    def test_manual_classification_of_pending_directory(self):
        self.folder("business")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scan_workspace.main(["--root", str(self.root), "--apply", "--select", "business", "--kind", "project"]), 0)
        self.assertEqual(project_registry.load_projects(self.root)[0]["path"], "business")

    def test_id_collision_does_not_drop_directory(self):
        self.folder("A B", "package.json")
        self.folder("A-B", "package.json")
        r = self.scan()
        self.assertEqual(len({p["id"] for p in r["projects"]}), 2)
        scan_workspace.register_scanned(self.root, r, apply=True)
        self.assertEqual(len(project_registry.load_projects(self.root)), 2)

    def test_missing_or_invalid_config_cannot_claim_success(self):
        self.folder("app", "package.json")
        r = self.scan()
        self.reg.unlink()
        with self.assertRaises((ValueError, OSError)):
            scan_workspace.register_scanned(self.root, r, apply=True)

    def test_failed_second_write_restores_first(self):
        self.folder("app", "package.json")
        self.folder("knowledge", "index.md")
        r = self.scan()
        before = (self.reg.read_bytes(), self.ws.read_bytes())
        import os
        real_replace = os.replace
        def fail_shared(src, dst):
            if Path(dst) == self.ws:
                raise OSError("simulated write failure")
            return real_replace(src, dst)
        with patch("tools.project_registry.os.replace", side_effect=fail_shared):
            with self.assertRaises(OSError):
                scan_workspace.register_scanned(self.root, r, apply=True)
        self.assertEqual((self.reg.read_bytes(), self.ws.read_bytes()), before)

    def test_block_lists_missing_code_and_existing_writer_compatibility(self):
        self.reg.write_text("exclude:\n- ignored/ # keep\nprojects:\n- id: app\n  path: app/\n  note: human # retain\n")
        self.folder("app/apps/api", "package.json")
        self.folder("new", "package.json")
        scan_workspace.register_scanned(self.root, self.scan(), apply=True)
        self.assertEqual(project_registry.load_projects(self.root)[0]["code"], ["app/apps/api"])
        self.assertTrue(project_registry.append_project(self.root, {"id": "manual", "path": "manual/"}))
        self.assertIn("# keep", self.reg.read_text())
        self.assertIn("# retain", self.reg.read_text())

    def test_pending_conflict_and_traversal_are_blocked(self):
        self.reg.write_text("exclude: [app/]\nprojects: [{id: app, path: app/}]\n")
        self.folder("app", "package.json")
        self.assertEqual(len(self.scan()["pending"]), 1)
        for selected in ("app", "../elsewhere"):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(scan_workspace.main(["--root", str(self.root), "--apply", "--select", selected, "--kind", "project"]), 0)

    def test_nested_project_has_its_own_engineering_boundary(self):
        self.reg.write_text("exclude: []\nprojects: [{id: parent, path: parent/}, {id: child, path: parent/apps/child/, parent: parent}]\n")
        self.folder("parent", "package.json")
        self.folder("parent/apps/child", "package.json")
        updates = {p["id"]: p["code"] for p in self.scan()["updates"]}
        self.assertEqual(updates["parent"], ["parent"])
        self.assertEqual(updates["child"], ["parent/apps/child"])

    def test_pagination(self):
        for i in range(3):
            self.folder(f"app{i}", "package.json")
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(scan_workspace.main(["--root", str(self.root), "--limit", "1", "--offset", "1"]), 0)
        lines = stream.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("status=preview "), lines)
        self.assertIn("projects=3", lines[0])
        self.assertEqual(sum(1 for line in lines if line.startswith("projects: ")), 1)
        self.assertIn("next_offset=2", lines)

    def test_invalid_config_and_anchor_refuse_write(self):
        self.folder("app", "package.json")
        r = self.scan()
        for text in ("exclude: wrong\nprojects: []\n", "exclude: &keep []\nprojects: []\n"):
            self.reg.write_text(text)
            with self.assertRaises((ValueError, TypeError)):
                scan_workspace.register_scanned(self.root, r, apply=True)
            self.assertEqual(self.reg.read_text(), text)

    def test_nonempty_flow_list_keeps_original_entries(self):
        text = "projects: [{id: manual, path: manual/, aliases: ['User']}] # retained\n"
        result = project_registry.append_yaml_items(text, "projects", [{"id": "new", "path": "new/"}])
        self.assertIn("{id: manual, path: manual/, aliases: ['User']}", result)
        self.assertIn("# retained", result)
        self.assertEqual(len(project_registry._yaml().safe_load(result)["projects"]), 2)

    def test_fresh_bootstrap_previews_and_rerun_preserves_registry(self):
        system = Path(__file__).resolve().parent.parent
        destination = self.root / ".entropaxis"
        for folder in ("tools", "entrypoints", "templates", "schemas", "skills"):
            shutil.copytree(system / folder, destination / folder, ignore=shutil.ignore_patterns("__pycache__"))
        # 模拟新克隆：模板实例不存在，由 bootstrap 补齐。
        self.reg.unlink()
        self.ws.unlink()
        self.folder("app", "package.json")
        command = [sys.executable, str(destination / "tools/bootstrap.py")]
        run = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("未登记", run.stdout)
        self.assertEqual(project_registry.load_projects(self.root), [])
        before = (self.reg.read_bytes(), self.ws.read_bytes())
        run = subprocess.run(command + ["--no-scan"], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("目录候选", run.stdout)
        self.assertEqual((self.reg.read_bytes(), self.ws.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
