import importlib.util
import json
import shutil
import sys
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

_SCRIPT = SYSTEM_ROOT / "skills" / "patent-combo" / "scripts" / "check_env.py"
_spec = importlib.util.spec_from_file_location("patent_combo_check_env", _SCRIPT)
check_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_env)

_FINALIZER = SYSTEM_ROOT / "skills" / "patent-combo" / "scripts" / "finalize_outputs.py"
_finalizer_spec = importlib.util.spec_from_file_location("patent_combo_finalize_outputs", _FINALIZER)
finalize_outputs = importlib.util.module_from_spec(_finalizer_spec)
_finalizer_spec.loader.exec_module(finalize_outputs)

_DISCLOSURE_TOOLS = (
    SYSTEM_ROOT / "skills" / "patent-combo" / "references" / "disclosure"
    / "skills" / "patent-disclosure" / "tools"
)
for _module_path in (_DISCLOSURE_TOOLS, _DISCLOSURE_TOOLS / "crawl"):
    if str(_module_path) not in sys.path:
        sys.path.insert(0, str(_module_path))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


formula_eval = _load_module("patent_formula_eval", _DISCLOSURE_TOOLS / "formula_eval.py")
cad_bootstrap = _load_module("patent_cad_bootstrap", _DISCLOSURE_TOOLS / "bootstrap_cad_venv.py")
cnipa_search = _load_module("patent_cnipa_search", _DISCLOSURE_TOOLS / "crawl" / "cnipa_epub_search.py")

CORE_FILES = [
    "scripts/finalize_outputs.py",
    "references/disclosure/skills/patent-disclosure/SKILL.md",
    "references/disclosure/skills/patent-disclosure/prompts/invention/disclosure_builder.md",
    "references/disclosure/skills/patent-disclosure/tools/md_to_docx.py",
    "references/disclosure/skills/patent-disclosure/tools/crawl/cnipa_epub_search.py",
    "references/claims-guide/PATENT_SKILL.md",
    "references/mining-rubric.md",
    "references/disclosure/skills/patent-disclosure/tools/vendor/mermaid.min.js",
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
            if rel.endswith("vendor/mermaid.min.js"):
                source = SYSTEM_ROOT / "skills" / "patent-combo" / rel
                p.write_bytes(source.read_bytes())
            else:
                p.write_text("placeholder", encoding="utf-8")
        self.out_dir = self.tmp / "ws" / "out"
        self.out_dir.mkdir()
        self.config = {"output_root": str(self.out_dir), "priorart_cli": "patent"}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_distribution_excludes_legacy_build_and_demo_files(self) -> None:
        package_root = SYSTEM_ROOT / "skills" / "patent-combo"
        excluded = (
            "references/disclosure/skills/patent-disclosure/tools/package.json",
            "references/disclosure/skills/patent-disclosure/tools/package-lock.json",
            "references/disclosure/skills/patent-disclosure/tools/gen_demo_snap_step.py",
        )
        for relative_path in excluded:
            self.assertFalse((package_root / relative_path).exists(), relative_path)

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
            importable_fn=lambda module: module != "playwright", which_fn=lambda name: None, env={},
        )
        self.assertTrue(report["core_ok"])  # Word 导出就绪时，Stage 4 降级不阻断核心阶段
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

    def test_missing_word_export_dependency_blocks(self) -> None:
        report = check_env.build_report(
            self.skill_dir, self.config, fix=False,
            importable_fn=lambda module: module not in {"docx", "latex2mathml", "yaml"},
            which_fn=lambda name: f"/usr/bin/{name}",
            env={},
        )
        self.assertFalse(report["core_ok"])
        word_export = next(c for c in report["checks"] if c["id"] == "word_export")
        self.assertEqual(word_export["status"], "missing")
        self.assertIn("python-docx", word_export["fix"])

    def test_fix_never_installs_runtime_dependencies(self) -> None:
        calls: list[list[str]] = []

        def installer(cmd: list[str]) -> tuple[bool, str]:
            calls.append(cmd)
            return True, ""

        report = check_env.build_report(
            self.skill_dir, self.config, fix=True,
            importable_fn=lambda module: module != "playwright",
            which_fn=lambda name: None, env={}, installer=installer,
        )
        by_id = {c["id"]: c for c in report["checks"]}
        self.assertEqual(by_id["playwright"]["status"], "missing")
        self.assertIn("自动安装已禁用", by_id["playwright"]["detail"])
        self.assertEqual(by_id["priorart_cli"]["status"], "missing")
        self.assertEqual(calls, [])

    def test_mermaid_hash_mismatch_blocks_core(self) -> None:
        mermaid = (
            self.skill_dir
            / "references/disclosure/skills/patent-disclosure/tools/vendor/mermaid.min.js"
        )
        mermaid.write_text("tampered", encoding="utf-8")

        report = check_env.build_report(
            self.skill_dir, self.config, fix=False,
            importable_fn=_ok, which_fn=lambda name: f"/usr/bin/{name}", env={},
        )

        self.assertFalse(report["core_ok"])
        integrity = next(c for c in report["checks"] if c["id"] == "mermaid_integrity")
        self.assertEqual(integrity["status"], "missing")
        self.assertIn("哈希不匹配", integrity["fix"])

    def test_legacy_overrides_reported(self) -> None:
        config = dict(self.config, disclosure_skill="/some/repo")
        report = check_env.build_report(
            self.skill_dir, config, fix=False,
            importable_fn=_ok, which_fn=lambda name: f"/usr/bin/{name}", env={},
        )
        legacy = next(c for c in report["checks"] if c["id"] == "legacy_overrides")
        self.assertIn("legacy", legacy["detail"].lower())



class PatentComboSecurityTests(TestCase):
    def test_formula_evaluator_uses_allowlisted_ast_interpreter(self) -> None:
        value, error = formula_eval.eval_rhs("min(8, 3) + 2 * 4", {})
        self.assertIsNone(error)
        self.assertEqual(value, 11.0)
        source = (_DISCLOSURE_TOOLS / "formula_eval.py").read_text(encoding="utf-8")
        self.assertNotIn("eval(compile(", source)

    def test_cad_cleanup_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tools = Path(temp) / "tools"
            tools.mkdir()
            external = Path(temp) / "external"
            external.mkdir()
            link = tools / "cad-env"
            link.symlink_to(external, target_is_directory=True)
            with patch.object(cad_bootstrap, "_SHARED", tools), patch.object(
                cad_bootstrap, "VENV_DIR", link
            ):
                with self.assertRaisesRegex(RuntimeError, "symlinked"):
                    cad_bootstrap._remove_stale_venv()
            self.assertTrue(external.is_dir())

    def test_cnipa_requires_public_term_acknowledgement(self) -> None:
        self.assertIn("public-terms-confirmed", cnipa_search._validate_public_terms(["路由"], False))
        self.assertIn("已阻断", cnipa_search._validate_public_terms(["token=secret"], True))
        self.assertIsNone(cnipa_search._validate_public_terms(["动作路由"], True))
class PatentComboFinalizerTests(TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="patent-combo-finalize-"))
        self.case_dir = self.tmp / "case"
        self.case_dir.mkdir()
        self.candidate = self.case_dir / "01_候选清单.md"
        self.disclosure = self.case_dir / "交底书工作稿.md"
        self.claims = self.case_dir / "权利要求工作稿.md"
        self.report = self.case_dir / "查新报告工作稿.md"
        self.candidate.write_text("# 候选清单\n", encoding="utf-8")
        self.disclosure.write_text(
            "# 交底书\n\n**专利类型**：发明\n\n"
            "## 3.2 系统框图\n\n```mermaid\nflowchart LR\nA-->B\n```\n\n"
            "## 3.4 系统流程说明\n\n```mermaid\nflowchart TD\nS1-->S2\n```\n\n"
            "## 3.6 关键实现代码\n\n```python\ndef route(prompt):\n    return prompt\n```\n",
            encoding="utf-8",
        )
        self.claims.write_text("# 权利要求\n", encoding="utf-8")
        self.report.write_text(
            "# 查新报告\n\n前言\n\n## 命中专利列表\n\n专利 A\n\n## 相似度判断\n\n低\n\n## 人工检索式清单\n\n不应交付\n",
            encoding="utf-8",
        )
        self.converter = self.tmp / "md_to_docx.py"
        self.converter.write_text("# placeholder\n", encoding="utf-8")
        self.renderer = self.tmp / "mermaid_render.py"
        self.renderer.write_text("# placeholder\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_extract_report_section_excludes_manual_queries(self) -> None:
        section = finalize_outputs.extract_report_section(self.report.read_text(encoding="utf-8"))
        self.assertTrue(section.startswith("## 命中专利列表"))
        self.assertIn("## 相似度判断", section)
        self.assertNotIn("人工检索式清单", section)

    def test_finalize_writes_named_docx_and_cleans_sources(self) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if "--no-docx" in command:
                source = Path(command[command.index("--input") + 1])
                rendered = Path(command[command.index("--output") + 1])
                figures = rendered.parent / "mermaid_figures"
                figures.mkdir()
                (figures / "fig_001.png").touch()
                (figures / "fig_002.png").touch()
                rendered.write_text(
                    source.read_text(encoding="utf-8")
                    + "\n<!-- ![图示 1](mermaid_figures/fig_001.png) -->\n"
                    + "<!-- ![图示 2](mermaid_figures/fig_002.png) -->\n",
                    encoding="utf-8",
                )
            else:
                Path(command[command.index("--output") + 1]).touch()
            return subprocess.CompletedProcess(command, 0, "", "")

        outputs = finalize_outputs.finalize(
            case_dir=self.case_dir,
            case_name="一种调度方法",
            candidate_md=self.candidate,
            disclosure_md=self.disclosure,
            claims_md=self.claims,
            report_md=self.report,
            converter=self.converter,
            renderer=self.renderer,
            runner=runner,
        )

        self.assertEqual(
            [p.name for p in outputs],
            ["01_交底书_一种调度方法.docx", "02_权利要求_一种调度方法.docx", "03_查新报告.docx"],
        )
        self.assertEqual(len(commands), 4)
        self.assertIn("--no-docx", commands[0])
        self.assertFalse(any(p.exists() for p in (self.candidate, self.disclosure, self.claims, self.report)))

    def test_failed_export_preserves_sources(self) -> None:
        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, "", "conversion failed")

        with self.assertRaisesRegex(RuntimeError, "conversion failed"):
            finalize_outputs.finalize(
                case_dir=self.case_dir,
                case_name="一种调度方法",
                candidate_md=self.candidate,
                disclosure_md=self.disclosure,
                claims_md=self.claims,
                report_md=self.report,
                converter=self.converter,
                runner=runner,
                renderer=self.renderer,
            )

        self.assertTrue(self.disclosure.exists())
        self.assertTrue(self.claims.exists())
        self.assertTrue(self.report.exists())

    def test_invention_without_two_diagrams_is_rejected(self) -> None:
        self.disclosure.write_text(
            "# 交底书\n\n**专利类型**：发明\n\n"
            "```mermaid\nflowchart LR\nA-->B\n```\n\n"
            "## 关键实现代码\n\n```python\nreturn True\n```\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "至少两张 Mermaid"):
            finalize_outputs.finalize(
                case_dir=self.case_dir,
                case_name="一种调度方法",
                candidate_md=self.candidate,
                disclosure_md=self.disclosure,
                claims_md=self.claims,
                report_md=self.report,
                converter=self.converter,
                renderer=self.renderer,
            )

    def test_invention_without_code_excerpt_is_rejected(self) -> None:
        self.disclosure.write_text(
            "# 交底书\n\n**专利类型**：发明\n\n"
            "```mermaid\nflowchart LR\nA-->B\n```\n\n"
            "```mermaid\nflowchart TD\nS1-->S2\n```\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "关键实现代码"):
            finalize_outputs.finalize(
                case_dir=self.case_dir,
                case_name="一种调度方法",
                candidate_md=self.candidate,
                disclosure_md=self.disclosure,
                claims_md=self.claims,
                report_md=self.report,
                converter=self.converter,
                renderer=self.renderer,
            )
