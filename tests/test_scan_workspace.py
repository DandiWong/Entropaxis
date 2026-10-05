"""目录发现、确认登记和人工配置保护的回归测试。"""
import contextlib
import io
import tempfile
import shutil
import subprocess
import sys
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import project_registry, scan_workspace


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

    def test_names_and_documents_are_only_hints(self):
        p = self.folder("business")
        for i in range(4):
            (p / f"report{i}.pdf").write_text("")
        self.folder("assets-product", "package.json")
        self.folder("reporting", "pyproject.toml")
        self.assertEqual({p["path"] for p in self.scan()["projects"]}, {"assets-product", "reporting"})
        self.assertEqual(self.scan()["pending"][0]["path"], "business")

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
            self.assertEqual(scan_workspace.main(["--root", str(self.root), "--json"]), 0)
        self.assertEqual(self.reg.read_bytes(), before)
        self.assertFalse(scan_workspace.register_scanned(self.root, self.scan()))

    def test_apply_requires_selection(self):
        self.folder("app", "package.json")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(scan_workspace.main(["--root", str(self.root), "--apply"]), 0)

    def test_confirmed_registration_preserves_comments_and_is_idempotent(self):
        self.folder("app", "package.json")
        self.folder("knowledge", "index.md")
        args = ["--root", str(self.root), "--apply", "--select", "app", "--select", "knowledge", "--json"]
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

    def test_json_apply_and_pagination(self):
        for i in range(3):
            self.folder(f"app{i}", "package.json")
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(scan_workspace.main(["--root", str(self.root), "--json", "--limit", "1", "--offset", "1"]), 0)
        data = json.loads(stream.getvalue())
        self.assertEqual(data["counts"]["projects"], 3)
        self.assertEqual(len(data["candidates"]["projects"]), 1)
        self.assertFalse(data["applied"])

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
