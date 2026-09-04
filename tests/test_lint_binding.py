import tempfile
import unittest
from pathlib import Path

from tools.lint_workspace import check_rules_zero_system_binding

# 禁词字面量拆开拼接，避免测试源码自身命中零绑定检查
_TOKEN_MULTI = "mult" + "ica"
_TOKEN_BRAND = "fosun" + "pharma"
_TOKEN_LOCAL = "127.0" + ".0.1"


def _mk_control_plane(root: Path) -> Path:
    s = root / ".system"
    for sub in ("rules", "tools", "skills/demo", "templates", "tests", "entrypoints"):
        (s / sub).mkdir(parents=True, exist_ok=True)
    return s


class ZeroBindingLintTests(unittest.TestCase):
    def test_clean_control_plane_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = _mk_control_plane(root)
            (s / "rules" / "a.md").write_text("通用规则正文，零系统绑定。", encoding="utf-8")
            (s / "tools" / "a.py").write_text("print('ok')\n", encoding="utf-8")
            (s / "templates" / "t.md").write_text("模板 {{var}}", encoding="utf-8")
            self.assertEqual(check_rules_zero_system_binding(root), [])

    def test_binding_keywords_and_local_endpoints_are_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = _mk_control_plane(root)
            (s / "rules" / "a.md").write_text(f"调用 {_TOKEN_MULTI} 同步任务。", encoding="utf-8")
            (s / "tools" / "a.py").write_text(f'BASE = "http://{_TOKEN_LOCAL}:8080"\n', encoding="utf-8")
            (s / "entrypoints" / "AGENTS.md").write_text(f"路由 {_TOKEN_BRAND} 看板", encoding="utf-8")
            issues = check_rules_zero_system_binding(root)
            self.assertTrue(any(_TOKEN_MULTI in i for i in issues))
            self.assertTrue(any(_TOKEN_BRAND in i for i in issues))
            self.assertTrue(any("硬编码本地端点" in i for i in issues))


if __name__ == "__main__":
    unittest.main()
