import tempfile
import unittest
from pathlib import Path

from tools.lint_workspace import check_routing_integrity

_REGISTRY = """\
# 工作区项目注册表

## 项目映射表

| 项目 ID | 名称 | 主目录 | 外部看板映射 (Key-Value) | 口语关键词 |
|---|---|---|---|---|
| `demo` | 演示项目 | `04Demo/` | main=demo | 演示 |

## 排除规则

以下目录不纳入注册表：
- `repo/`、`Archive/`、`node_modules/`
- 共享资料层：`00_知识库/`
"""


def _mk_root(td: str) -> Path:
    root = Path(td)
    (root / ".data" / "templates").mkdir(parents=True, exist_ok=True)
    (root / ".data" / "templates" / "registry.md").write_text(_REGISTRY, encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
