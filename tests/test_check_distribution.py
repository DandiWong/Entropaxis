import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import paths
from tools.check_distribution import (
    ToolError,
    build_recipient_tree,
    distribution_files,
    pending_files,
    scan_leaks,
    FOREIGN_DIR_PROBE,
    dangling_skill_routes,
)

_TERMS = ["acmecorp", "acmeboard"]

# 夹具字面量一律拆开拼接：本文件自身会进入版本库，被 check_distribution 扫描，
# 直写会让工具把自己的测试报成凭据泄露与家目录路径（同 test_lint_binding.py 惯例）。
_FAKE_CRED = 'API_' + 'KEY = "abcdefghijklmnop0123"\n'
_FAKE_HOME = "/Us" + "ers/someone/Work/x.md"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


def _mk_repo(root: Path) -> Path:
    """最小 .entropaxis 仓库：git init + add，无需 commit（ls-files 读暂存区即可）。"""
    system = root / paths.SYSTEM_DIRNAME
    (system / "skills").mkdir(parents=True)
    _git(system, "init", "-q")
    return system


class DistributionScanTests(unittest.TestCase):
    def test_entity_name_and_credential_and_home_path_are_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tree = Path(td)
            (tree / "a.md").write_text("路由到 acmecorp 看板。", encoding="utf-8")
            (tree / "b.py").write_text(_FAKE_CRED, encoding="utf-8")
            (tree / "c.md").write_text(f"见 {_FAKE_HOME}", encoding="utf-8")
            issues = scan_leaks(tree, _TERMS)
            self.assertTrue(any("实体名泄露" in i for i in issues), issues)
            self.assertTrue(any("疑似凭据" in i for i in issues), issues)
            self.assertTrue(any("家目录绝对路径" in i for i in issues), issues)

    def test_plain_token_variable_is_not_mistaken_for_credential(self) -> None:
        """解析器里的普通变量名不得触发凭据告警——宽匹配会把真信号淹掉。"""
        with tempfile.TemporaryDirectory() as td:
            tree = Path(td)
            (tree / "parser.py").write_text(
                "token = payload[0]\nif token.startswith('**'):\n    pass\n", encoding="utf-8"
            )
            self.assertEqual(scan_leaks(tree, _TERMS), [])

    def test_workspace_residue_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tree = Path(td)
            (tree / ".DS_Store").write_bytes(b"\x00\x01")
            (tree / "__pycache__").mkdir()
            (tree / "__pycache__" / "x.cpython-314.pyc").write_bytes(b"\x00")
            issues = scan_leaks(tree, _TERMS)
            self.assertEqual(len(issues), 2, issues)
            self.assertTrue(all("工作区残渣" in i for i in issues), issues)

    def test_binary_assets_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tree = Path(td)
            (tree / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n binary acmecorp bytes")
            self.assertEqual(scan_leaks(tree, _TERMS), [])


class DistributionSetTests(unittest.TestCase):
    def test_ignored_file_is_excluded_from_distribution(self) -> None:
        """物理存在 ≠ 会被分发：被忽略的私有内容不进跟踪集，因此不参与泄露判定。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            system = _mk_repo(root)
            (system / "skills" / "private-skill").mkdir()
            (system / "skills" / "private-skill" / ".gitignore").write_text("*\n", encoding="utf-8")
            (system / "skills" / "private-skill" / "s.py").write_text("acmecorp", encoding="utf-8")
            (system / "public.md").write_text("公开内容", encoding="utf-8")
            _git(system, "add", "-A")

            files = distribution_files(system)
            self.assertIn("public.md", files)
            self.assertFalse(any("private-skill" in f for f in files), files)

            dest = root / "out"
            build_recipient_tree(system, files, dest)
            self.assertFalse((dest / paths.SYSTEM_DIRNAME / "skills" / "private-skill").exists())
            self.assertEqual(scan_leaks(dest / paths.SYSTEM_DIRNAME, _TERMS), [])
            # 收件方工作区非空：只放 .entropaxis/ 空壳树求值不到"既有目录未注册"这类缺陷
            self.assertTrue((dest / FOREIGN_DIR_PROBE).is_dir())
            # 探针只播在工作区根，不得混进 .entropaxis/（否则会被当成分发内容扫描）
            self.assertFalse((dest / paths.SYSTEM_DIRNAME / FOREIGN_DIR_PROBE).exists())

    def test_untracked_file_reported_as_pending(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            system = _mk_repo(root)
            (system / "tracked.md").write_text("x", encoding="utf-8")
            _git(system, "add", "-A")
            (system / "forgotten.md").write_text("y", encoding="utf-8")
            self.assertEqual(pending_files(system), ["forgotten.md"])

    def test_private_skill_referenced_by_shipped_rule_is_flagged(self) -> None:
        """版本库里留下指向私有 Skill 的指令 = 收件方拿到一条没有执行体的路由。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            system = _mk_repo(root)
            for name in ("shipped", "local-only"):
                (system / "skills" / name).mkdir()
            tree = root / "tree" / paths.SYSTEM_DIRNAME
            (tree / "rules").mkdir(parents=True)
            (tree / "rules" / "r.md").write_text("调用 local-only Skill 执行。", encoding="utf-8")
            issues = dangling_skill_routes(system, tree, ["skills/shipped/SKILL.md"])
            self.assertEqual(len(issues), 1, issues)
            self.assertIn("local-only", issues[0])

    def test_cleanly_encapsulated_private_skill_is_silent(self) -> None:
        """私有 Skill 不随分发本身是设计，无人引用时不报——避免把它变成噪声与私有能力清单。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            system = _mk_repo(root)
            for name in ("shipped", "local-only"):
                (system / "skills" / name).mkdir()
            tree = root / "tree" / paths.SYSTEM_DIRNAME
            (tree / "rules").mkdir(parents=True)
            (tree / "rules" / "r.md").write_text("通用规则正文，不引用任何私有能力。", encoding="utf-8")
            self.assertEqual(dangling_skill_routes(system, tree, ["skills/shipped/SKILL.md"]), [])

    def test_non_git_directory_raises_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ToolError) as ctx:
                distribution_files(Path(td))
            self.assertIn("👉", str(ctx.exception))

    def test_empty_tracked_set_raises_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(ToolError) as ctx:
                build_recipient_tree(root, [], root / "out")
            self.assertIn("👉", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
