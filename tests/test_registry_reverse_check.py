import tempfile
import unittest
from pathlib import Path

from tools import paths
from tools.lint_workspace import check_routing_integrity, check_schema_conformance

_REGISTRY = """\
exclude: [repo/, Archive/, node_modules/, 00_知识库/]
projects:
  - {id: demo, name: 演示项目, path: 04Demo/, aliases: [演示], boards: {main: demo}}
"""


def _mk_root(td: str) -> Path:
    root = Path(td)
    (root / paths.SYSTEM_DIRNAME / "data" / "templates").mkdir(parents=True, exist_ok=True)
    (root / paths.SYSTEM_DIRNAME / "data" / "templates" / "registry.yaml").write_text(_REGISTRY, encoding="utf-8")
    return root


class RegistryReverseCheckTests(unittest.TestCase):
    def test_registered_and_excluded_dirs_pass(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _mk_root(td)
            for d in ("04Demo", "repo", "Archive", "00_知识库", ".hidden"):
                (root / d).mkdir()
            issues = check_routing_integrity(root)
            self.assertFalse(any("根目录未注册" in i for i in issues))

    def test_unregistered_dir_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _mk_root(td)
            (root / "04Demo").mkdir()
            (root / "游离项目").mkdir()
            issues = check_routing_integrity(root)
            self.assertTrue(any("游离项目" in i and "根目录未注册" in i for i in issues))

    def test_malformed_registry_violates_schema(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _mk_root(td)
            (root / paths.SYSTEM_DIRNAME / "tools").symlink_to(Path(paths.__file__).resolve().parent)
            (root / paths.SYSTEM_DIRNAME / "data" / "templates" / "registry.yaml").write_text(
                "projects:\n  - {id: demo, dir: 04Demo/}\n", encoding="utf-8")
            issues = check_schema_conformance(root)
            self.assertTrue(any("registry.yaml" in i and "path" in i for i in issues), issues)


if __name__ == "__main__":
    unittest.main()
