import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

_SCRIPT = SYSTEM_ROOT / "skills" / "patent-combo" / "scripts" / "check_env.py"
_spec = importlib.util.spec_from_file_location("patent_combo_check_env", _SCRIPT)
check_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_env)

CORE_FILES = [
    "references/disclosure/skills/patent-disclosure/SKILL.md",
    "references/disclosure/skills/patent-disclosure/prompts/invention/disclosure_builder.md",
    "references/disclosure/skills/patent-disclosure/tools/md_to_docx.py",
    "references/disclosure/skills/patent-disclosure/tools/crawl/cnipa_epub_search.py",
    "references/claims-guide/PATENT_SKILL.md",
    "references/mining-rubric.md",
]


def _ok(module: str) -> bool:
    return True


def _missing(module: str) -> bool:
    return False


class PatentComboEnvTests(TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="patent-combo-env-"))
        self.skill_dir = self.tmp / "ws" / ".system" / "skills" / "patent-combo"
        for rel in CORE_FILES:
            p = self.skill_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("placeholder", encoding="utf-8")
        self.out_dir = self.tmp / "ws" / "out"
        self.out_dir.mkdir()
        self.config = {"output_root": str(self.out_dir), "priorart_cli": "patent"}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_ready(self) -> None:
        report = check_env.build_report(
            self.skill_dir, self.config, fix=False,
            importable_fn=_ok, which_fn=lambda name: f"/usr/bin/{name}", env={},
        )
        self.assertTrue(report["core_ok"])
        self.assertEqual(len(report["ready_stages"]), 4)
        self.assertEqual(report["degraded_stages"], [])

    def test_missing_optional_deps_yield_degradation_plans(self) -> None:
        report = check_env.build_report(
            self.skill_dir, self.config, fix=False,
            importable_fn=_missing, which_fn=lambda name: None, env={},
        )
        self.assertTrue(report["core_ok"])  # 增量依赖缺失不阻断核心阶段
        self.assertIn("Stage 4 CNIPA 路（人工检索包）", report["degraded_stages"])
        self.assertIn("Stage 4 dev-tool 路（搜索级/未执行）", report["degraded_stages"])
        by_id = {c["id"]: c for c in report["checks"]}
        self.assertIn("人工检索包", by_id["playwright"]["degrade"])
        self.assertIn("--fast --keyword-only", by_id["verdict_backend"]["degrade"])
        for c in report["checks"]:
            if c["status"] != "ok":
                self.assertTrue(c.get("fix"), f"{c['id']} 缺修复建议")

    def test_missing_core_asset_blocks(self) -> None:
        (self.skill_dir / "references/claims-guide/PATENT_SKILL.md").unlink()
        report = check_env.build_report(
            self.skill_dir, self.config, fix=False,
            importable_fn=_ok, which_fn=lambda name: f"/usr/bin/{name}", env={},
        )
        self.assertFalse(report["core_ok"])
        self.assertEqual(report["ready_stages"], [])

    def test_fix_installs_playwright(self) -> None:
        calls: list[list[str]] = []
        state = {"installed": False}

        def importable(module: str) -> bool:
            return module != "playwright" or state["installed"]

        def installer(cmd: list[str]) -> tuple[bool, str]:
            calls.append(cmd)
            state["installed"] = True
            return True, ""

        report = check_env.build_report(
            self.skill_dir, self.config, fix=True,
            importable_fn=importable, which_fn=lambda name: f"/usr/bin/{name}",
            env={}, installer=installer,
        )
        by_id = {c["id"]: c for c in report["checks"]}
        self.assertEqual(by_id["playwright"]["status"], "ok")
        self.assertEqual(len(calls), 1)
        self.assertIn("playwright", calls[0][-1])

    def test_priorart_cli_cargo_install(self) -> None:
        calls: list[list[str]] = []
        paths: dict[str, str | None] = {"cargo": "/usr/local/bin/cargo", "patent": None}

        def which_fn(name: str) -> str | None:
            return paths.get(name)

        def installer(cmd: list[str]) -> tuple[bool, str]:
            calls.append(cmd)
            paths["patent"] = "/Users/x/.cargo/bin/patent"
            return True, ""

        report = check_env.build_report(
            self.skill_dir, self.config, fix=True,
            importable_fn=_ok, which_fn=which_fn, env={}, installer=installer,
        )
        by_id = {c["id"]: c for c in report["checks"]}
        self.assertEqual(by_id["priorart_cli"]["status"], "ok")
        self.assertEqual(len(calls), 1)
        self.assertIn("cargo", calls[0])
        self.assertIn("patent", calls[0])

    def test_legacy_overrides_reported(self) -> None:
        config = dict(self.config, disclosure_skill="/some/repo")
        report = check_env.build_report(
            self.skill_dir, config, fix=False,
            importable_fn=_ok, which_fn=lambda name: f"/usr/bin/{name}", env={},
        )
        legacy = next(c for c in report["checks"] if c["id"] == "legacy_overrides")
        self.assertIn("legacy", legacy["detail"].lower())
