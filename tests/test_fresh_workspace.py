"""全新工作区（收件方形态）可用性契约。

验收场景：一台全新机器，工作区的目录结构、命名与内容与任何既有工作区都毫无交集。
这类环境下的三条曾经断掉的链路：
  1. 首次体检直接红屏（拿空注册表去判用户既有目录"未注册"）；
  2. 共享资料层渲染出某个特定工作区的目录名（把推断写成事实，且属零系统绑定违规）；
     现默认为空——跨项目共享目录是少数形态，各使用者目录各不相同，不探测也不追问；
  3. 注册表声明 init-project 是唯一写入者，实际无人写——项目索引永远建不起来。
"""

import tempfile
import unittest
from pathlib import Path

from tools import paths
from tools.bootstrap import render_instance_configs
from tools.init_project import register_project
from tools.lint_workspace import (
    _is_first_party,
    check_registry_population,
    check_routing_integrity,
)

_REGISTRY = """# 工作区项目注册表

## 项目映射表

| 项目 ID | 名称 | 主目录 | 外部看板映射 (Key-Value) | 备注 |
|---|---|---|---|---|
| （待填写） | （项目全称） | （主目录路径） | main=<ID> | |

## 排除规则

- `repo/`、`Archive/`、`node_modules/`
"""


def _mk_workspace(root: Path, *dirs: str) -> Path:
    for d in dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    registry = root / paths.SYSTEM_DIRNAME / "data" / "templates" / "registry.md"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(_REGISTRY, encoding="utf-8")
    return registry


class FreshWorkspaceTests(unittest.TestCase):
    def test_unpopulated_registry_does_not_block_first_lint(self) -> None:
        """注册表空表时既有目录只进建议项——收件方首次体检不得红屏。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_workspace(root, "Clients", "Research", "notebooks")
            self.assertEqual(check_routing_integrity(root), [])
            advisory = check_registry_population(root)
            self.assertEqual(len(advisory), 1, advisory)
            for name in ("Clients", "Research", "notebooks"):
                self.assertIn(name, advisory[0])

    def test_populated_registry_hands_back_to_blocking_check(self) -> None:
        """登记任一项目后建议项让位，反向校验恢复为阻断——门禁不会被永久关掉。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_workspace(root, "Clients", "声学实验台")
            self.assertTrue(register_project(root, "声学实验台"))
            self.assertEqual(check_registry_population(root), [])
            blocking = check_routing_integrity(root)
            self.assertTrue(any("Clients/" in i for i in blocking), blocking)
            self.assertFalse(any("声学实验台" in i for i in blocking), blocking)

    def test_excluded_dirs_never_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_workspace(root, "Archive", "node_modules")
            self.assertEqual(check_registry_population(root), [])

    def test_register_project_is_append_only_and_idempotent(self) -> None:
        """`data/` 写入规约：只追加，不重写；重复立项不产生第二行。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = _mk_workspace(root, "proj-a")
            self.assertTrue(register_project(root, "proj-a", "proj_77"))
            self.assertFalse(register_project(root, "proj-a", "proj_77"))
            text = registry.read_text(encoding="utf-8")
            self.assertEqual(text.count("`proj-a/`"), 1)
            self.assertIn("main=proj_77", text)
            # 占位行与排除规则原样保留
            self.assertIn("（待填写）", text)
            self.assertIn("## 排除规则", text)

    def test_register_project_without_registry_does_not_raise(self) -> None:
        """注册表尚未 bootstrap 时立项不得失败——脚手架不依赖注册表存在。"""
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(register_project(Path(td), "proj-a"))

    def test_shared_dir_layer_renders_empty_by_default(self) -> None:
        """共享资料层默认留空：既不回填任何具体目录名，也不把工作区目录塞进去待确认。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for d in ("Clients", "Research"):
                (root / d).mkdir()
            templates = root / paths.SYSTEM_DIRNAME / "templates" / "instance"
            templates.mkdir(parents=True)
            (templates / "workspace-config.template.md").write_text(
                "| `{{ORG_SHARED_DIR_1}}` | {{ORG_SHARED_PURPOSE_1}} |\n"
                "| `{{ORG_SHARED_DIR_2}}` | {{ORG_SHARED_PURPOSE_2}} |\n"
                "| `{{ORG_SHARED_DIR_3}}` | {{ORG_SHARED_PURPOSE_3}} |\n",
                encoding="utf-8",
            )
            render_instance_configs(
                verbose=False,
                templates_dir=root / paths.SYSTEM_DIRNAME / "templates",
                data_dir=root / paths.SYSTEM_DIRNAME / "data",
                ws_name="anybox",
            )
            out = (root / paths.SYSTEM_DIRNAME / "data" / "templates" / "workspace-config.md").read_text(encoding="utf-8")
            self.assertNotIn("Clients", out)
            self.assertNotIn("Research", out)
            self.assertNotIn("待确认", out)
            self.assertEqual(out.count("默认无"), 3)
            self.assertNotIn("{{", out)


class ThirdPartyExclusionTests(unittest.TestCase):
    """上游/第三方嵌套仓库不纳入体检——其内容契约归它自己的 AGENTS.md。

    此前靠在 EXCLUDE_PATTERNS 里硬编码一个具体仓库目录名兜住：既是零系统绑定违规，
    也只兜得住那一个工作区；收件方 vendor 进来的任何上游仓库都会被当成待规范化的
    项目薄壳，报出一堆它无权修改的违规。
    """

    def test_nested_git_repo_is_not_first_party(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vendored = root / "vendor" / "upstream-lib"
            (vendored / ".git").mkdir(parents=True)
            (vendored / "sub").mkdir()
            self.assertFalse(_is_first_party(vendored / "sub" / "AGENTS.md", root))

    def test_own_project_is_first_party(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "myproj").mkdir()
            self.assertTrue(_is_first_party(root / "myproj" / "AGENTS.md", root))

    def test_named_exclusions_still_apply(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Archive").mkdir()
            self.assertFalse(_is_first_party(root / "Archive" / "AGENTS.md", root))


if __name__ == "__main__":
    unittest.main()
