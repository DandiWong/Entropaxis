import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from tools.init_capsule import (
    CapsuleInitError,
    _parsed_topic,
    _self_check,
    create_capsule,
    main,
)


class CreateCapsuleModeTests(TestCase):
    """三模式生成的文件集必须与规范 §2.3 分流表完全一致（R2-m5/R4-M1）。"""

    def test_full_mode_generates_all_stages_and_no_empty_dirs(self) -> None:
        with TemporaryDirectory() as base:
            target = create_capsule(Path(base), "全量测试方案", "Tech-1", "full")
            names = {p.name for p in target.iterdir()}
            self.assertEqual(
                names,
                {"01_调研.md", "02_方案.md", "03_设计.md", "04_Spec_Tech-1.md", "07_验收报告.md", "capsule.yaml"},
            )
            self.assertFalse((target / "00_原始素材").exists())
            self.assertFalse((target / "assets").exists())

    def test_light_mode_skips_design_stage(self) -> None:
        with TemporaryDirectory() as base:
            target = create_capsule(Path(base), "轻量测试方案", "Tech-2", "light")
            names = {p.name for p in target.iterdir()}
            self.assertEqual(
                names,
                {"01_调研.md", "02_方案.md", "04_Spec_Tech-2.md", "07_验收报告.md", "capsule.yaml"},
            )

    def test_research_mode_only_generates_research_doc(self) -> None:
        with TemporaryDirectory() as base:
            target = create_capsule(Path(base), "调研测试方案", "Tech-3", "research")
            names = {p.name for p in target.iterdir()}
            self.assertEqual(names, {"01_调研.md", "capsule.yaml"})

    def test_no_audit_report_ever_generated(self) -> None:
        """R2-C1：脚手架不得预生成审计报告，审计报告只能由「审计」指令按真实指纹创建。"""
        with TemporaryDirectory() as base:
            for mode, task_id in (("full", "Tech-4"), ("light", "Tech-5"), ("research", "Tech-6")):
                target = create_capsule(Path(base), f"{mode}审计测试方案", task_id, mode)
                names = {p.name for p in target.iterdir()}
                self.assertFalse(any("审计" in n or "Audit" in n for n in names))


class FrontMatterFactContractTests(TestCase):
    """R2-M2：所有阶段文档初始必须是 draft，且不得预填未发生的交付/测试事实。"""

    def test_all_generated_docs_status_draft(self) -> None:
        with TemporaryDirectory() as base:
            target = create_capsule(Path(base), "状态测试方案", "Tech-7", "full")
            for md in target.glob("*.md"):
                text = md.read_text(encoding="utf-8")
                self.assertIn("status: draft", text, f"{md.name} 不是 draft")

    def test_acceptance_report_has_no_prefilled_facts(self) -> None:
        with TemporaryDirectory() as base:
            target = create_capsule(Path(base), "验收测试方案", "Tech-8", "full")
            text = (target / "07_验收报告.md").read_text(encoding="utf-8")
            self.assertIn("待回填", text)
            self.assertNotIn("测试回归：全部通过", text)
            self.assertNotIn("status: delivered", text)


class CapsuleManifestTests(TestCase):
    """R2-M1/R4-M1：胶囊级 lifecycle 与文档级 status 分层；instantiated_stages 与实际文件集恒等。"""

    def test_manifest_lifecycle_separated_from_doc_status(self) -> None:
        with TemporaryDirectory() as base:
            target = create_capsule(Path(base), "清单分层方案", "Tech-9", "full")
            manifest = (target / "capsule.yaml").read_text(encoding="utf-8")
            self.assertIn("lifecycle: draft", manifest)

    def test_instantiated_stages_matches_actual_files(self) -> None:
        with TemporaryDirectory() as base:
            target = create_capsule(Path(base), "阶段一致方案", "Tech-10", "light")
            manifest = (target / "capsule.yaml").read_text(encoding="utf-8")
            line = next(l for l in manifest.splitlines() if l.startswith("instantiated_stages:"))
            self.assertEqual(line, "instantiated_stages: [01_调研, 02_方案, 04_Spec, 07_验收]")


class TopicYamlSafetyTests(TestCase):
    """R5-M1：主题含 YAML 指示字符时不得被静默截断，写入值须能反解析回原始输入。"""

    def test_hash_character_survives_roundtrip(self) -> None:
        topic = "清单 #截断"
        with TemporaryDirectory() as base:
            target = create_capsule(Path(base), topic, "Tech-11", "research")
            manifest = (target / "capsule.yaml").read_text(encoding="utf-8")
            line = next(l for l in manifest.splitlines() if l.startswith("topic:"))
            self.assertEqual(_parsed_topic(line.split(":", 1)[1].strip(), "capsule.yaml"), topic)
            doc = (target / "01_调研.md").read_text(encoding="utf-8")
            fm_topic_line = next(l for l in doc.splitlines() if l.startswith("topic:"))
            self.assertEqual(_parsed_topic(fm_topic_line.split(":", 1)[1].strip(), "01_调研.md"), topic)

    def test_multiple_yaml_indicator_characters_survive_roundtrip(self) -> None:
        topic = "清单 #井号 &锚点 [列表] {映射}"
        with TemporaryDirectory() as base:
            target = create_capsule(Path(base), topic, "Tech-12", "research")
            manifest = (target / "capsule.yaml").read_text(encoding="utf-8")
            line = next(l for l in manifest.splitlines() if l.startswith("topic:"))
            self.assertEqual(_parsed_topic(line.split(":", 1)[1].strip(), "capsule.yaml"), topic)

    def test_self_check_rejects_corrupted_topic(self) -> None:
        """直接单测防御本身：写入值与原始输入不一致时 _self_check 必须拦截。"""
        with TemporaryDirectory() as base:
            target = create_capsule(Path(base), "自检防御方案", "Tech-13", "research")
            manifest_path = target / "capsule.yaml"
            corrupted = manifest_path.read_text(encoding="utf-8").replace(
                '"自检防御方案"', '"已损坏"'
            )
            manifest_path.write_text(corrupted, encoding="utf-8")
            with self.assertRaises(CapsuleInitError):
                _self_check(target, "Tech-13", "自检防御方案")


class InputValidationTests(TestCase):
    """R2-m1：路径合法性强校验先于任何目录操作，且报错含行动导向修复建议（ApX 契约）。"""

    def test_rejects_non_chinese_topic(self) -> None:
        with TemporaryDirectory() as base:
            with self.assertRaises(CapsuleInitError) as ctx:
                create_capsule(Path(base), "EnglishOnly", "Tech-1", "full")
            self.assertIn("❌", str(ctx.exception))
            self.assertIn("👉", str(ctx.exception))

    def test_rejects_reserved_character_slash(self) -> None:
        with TemporaryDirectory() as base:
            with self.assertRaises(CapsuleInitError):
                create_capsule(Path(base), "子目录/中文主题", "Tech-1", "full")

    def test_rejects_path_traversal_in_id(self) -> None:
        with TemporaryDirectory() as base:
            with self.assertRaises(CapsuleInitError):
                create_capsule(Path(base), "穿越测试方案", "../../Tech-1", "full")

    def test_rejects_leading_trailing_whitespace(self) -> None:
        with TemporaryDirectory() as base:
            with self.assertRaises(CapsuleInitError):
                create_capsule(Path(base), " 首尾空格方案 ", "Tech-1", "full")

    def test_rejects_malformed_task_id(self) -> None:
        with TemporaryDirectory() as base:
            with self.assertRaises(CapsuleInitError):
                create_capsule(Path(base), "格式错误方案", "not-an-id", "full")


class AtomicityTests(TestCase):
    """R2-m2：no-clobber 且失败路径不留半成品。"""

    def test_no_clobber_existing_target(self) -> None:
        with TemporaryDirectory() as base:
            create_capsule(Path(base), "重复测试方案", "Tech-1", "research")
            with self.assertRaises(CapsuleInitError):
                create_capsule(Path(base), "重复测试方案", "Tech-2", "research")

    def test_failure_leaves_no_temp_residue(self) -> None:
        with TemporaryDirectory() as base:
            with self.assertRaises(CapsuleInitError):
                create_capsule(Path(base), "残留测试方案", "../../Tech-1", "full")
            entries = list(Path(base).iterdir())
            self.assertEqual(entries, [], f"预期无残留，实得: {entries}")


class MainJsonOutputTests(TestCase):
    """SOP Phase 4：--json 输出必须是可解析的结构化 JSON。"""

    def test_json_flag_emits_parseable_structured_result(self) -> None:
        with TemporaryDirectory() as base:
            argv = [str(Path(base)), "命令行测试方案", "--id", "Tech-14", "--mode", "research", "--json"]
            original_argv = sys.argv
            sys.argv = ["init_capsule.py", *argv]
            buffer = io.StringIO()
            try:
                with redirect_stdout(buffer):
                    exit_code = main()
            finally:
                sys.argv = original_argv
            self.assertEqual(exit_code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["lifecycle"], "draft")
            self.assertEqual(payload["mode"], "research")
            self.assertTrue(Path(payload["target"]).is_dir())

    def test_invalid_input_returns_nonzero_without_json_stdout_pollution(self) -> None:
        with TemporaryDirectory() as base:
            argv = [str(Path(base)), "EnglishOnly", "--json"]
            original_argv = sys.argv
            sys.argv = ["init_capsule.py", *argv]
            buffer = io.StringIO()
            try:
                with redirect_stdout(buffer):
                    exit_code = main()
            finally:
                sys.argv = original_argv
            self.assertEqual(exit_code, 1)
            self.assertEqual(buffer.getvalue(), "")
