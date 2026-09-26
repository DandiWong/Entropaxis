"""Skill 对根系统零依赖契约（《技能设计》2.2 独立运行铁律）。

Skill 要能脱离本工作区、以任意方式安装后独立运行。`.entropaxis/tools/x.py` 在只装了
一个 Skill 的机器上根本不存在——把它写成无条件前置，Skill 换个地方就跑不起来。
同时 `open_file.py` 仍由 `.entropaxis/` 单点掌管：允许"有就调、没有就降级"，
禁止的是硬依赖，也禁止 Skill 自建第二份打开实现（后者由本文件之外的规则正文约束）。
"""

import re
import tempfile
import unittest
from pathlib import Path

from tools import paths
from tools.lint_workspace import check_skill_system_independence

SYSTEM_ROOT = Path(__file__).resolve().parent.parent


def _mk_skill(root: Path, name: str, body: str, front_extra: str = "") -> None:
    d = root / paths.SYSTEM_DIRNAME / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: \"x\"\n{front_extra}---\n\n{body}\n", encoding="utf-8"
    )


class SkillIndependenceTests(unittest.TestCase):
    def test_unconditional_system_tool_call_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_skill(root, "demo", '交付前执行 `python3 .entropaxis/tools/open_file.py "<path>"`。')
            issues = check_skill_system_independence(root)
            self.assertEqual(len(issues), 1, issues)
            self.assertIn("根系统硬依赖", issues[0])

    def test_optional_marker_on_same_line_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_skill(root, "demo", "若工作区提供 `.entropaxis/tools/open_file.py` 就调用它，否则回报绝对路径。")
            self.assertEqual(check_skill_system_independence(root), [])

    def test_optional_marker_on_preceding_comment_passes(self) -> None:
        """注释写在命令上一行是惯用位置，不该因此判违规。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_skill(
                root, "demo",
                "```bash\n# 仅在 .entropaxis/tools/open_file.py 存在时执行\n"
                'python3 .entropaxis/tools/open_file.py "<path>"\n```',
            )
            self.assertEqual(check_skill_system_independence(root), [])

    def test_control_plane_skill_is_exempt(self) -> None:
        """以 .entropaxis/ 为作业对象的 Skill 按定义不独立分发，由 frontmatter 自声明豁免。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_skill(
                root, "forge", "运行 `python3 .entropaxis/tools/lint_workspace.py` 验证。",
                front_extra="metadata:\n  scope: control-plane\n",
            )
            self.assertEqual(check_skill_system_independence(root), [])

    def test_private_untracked_skill_is_still_checked(self) -> None:
        """私有 Skill 同样要独立分发与独立运行，不因不进版本库而豁免。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / paths.SYSTEM_DIRNAME / "skills" / "private"
            d.mkdir(parents=True)
            (d / ".gitignore").write_text("*\n", encoding="utf-8")
            _mk_skill(root, "private", '执行 `python3 .entropaxis/tools/open_file.py "<p>"`。')
            self.assertEqual(len(check_skill_system_independence(root)), 1)

    def test_self_path_inside_skills_is_not_flagged(self) -> None:
        """`.entropaxis/skills/<自身名>/…` 是安装位置示例，常与全局安装形式并列，不构成本项违规。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mk_skill(root, "demo", "全局安装用 `<skill-dir>/scripts/x.py`，工作区内为 `.entropaxis/skills/demo/scripts/x.py`。")
            self.assertEqual(check_skill_system_independence(root), [])


if __name__ == "__main__":
    unittest.main()


class LegacyDataPathTests(unittest.TestCase):
    """实例数据只在 `.entropaxis/data/`；旧布局 `.data/`、`.system/` 已不存在，
    Skill（含本机私有 Skill）写死旧路径会静默读空、退到全局兜底。"""

    LEGACY = re.compile(r"(?<![\w.])\.(?:data|system)/|[\"']\.(?:data|system)[\"']")

    def test_no_skill_uses_legacy_layout_paths(self) -> None:
        hits = []
        for p in (SYSTEM_ROOT / "skills").rglob("*"):
            if p.suffix not in (".md", ".py", ".sh") or p.name == "CHANGELOG.md" or not p.is_file():
                continue
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if self.LEGACY.search(line):
                    hits.append(f"{p.relative_to(SYSTEM_ROOT)}:{n}")
        self.assertEqual(hits, [], "改为 .entropaxis/data/ 或相对自身目录定位")


class SelfContainedConfigTests(unittest.TestCase):
    """非控制面 Skill 须自包含：配置随自身目录、凭据放用户级目录，不读写 .entropaxis/data/（《技能设计》2.2）。"""

    def test_skills_do_not_reference_control_plane_data(self) -> None:
        hits = []
        for skill in sorted(p for p in (SYSTEM_ROOT / "skills").iterdir() if p.is_dir()):
            md = skill / "SKILL.md"
            if not md.is_file():
                continue
            if "scope: control-plane" in md.read_text(encoding="utf-8"):
                continue
            for p in skill.rglob("*"):
                if p.suffix in (".md", ".py", ".sh", ".ps1") and p.name != "CHANGELOG.md" and p.is_file():
                    if ".entropaxis/data" in p.read_text(encoding="utf-8", errors="ignore"):
                        hits.append(str(p.relative_to(SYSTEM_ROOT)))
        self.assertEqual(hits, [], "配置放 Skill 自身目录，凭据放用户级目录或环境变量")
