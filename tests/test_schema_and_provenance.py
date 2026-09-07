import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

import check_audit_gate as GATE  # noqa: E402
import stamp_data_provenance as PROV  # noqa: E402
import validate_schema as VS  # noqa: E402


class SchemaValidatorTests(TestCase):
    def test_type_mismatch(self) -> None:
        self.assertTrue(VS.validate("x", {"type": "integer"}))
        self.assertFalse(VS.validate(3, {"type": "integer"}))

    def test_bool_is_not_integer(self) -> None:
        """bool 是 int 的子类，若不显式排除，true 会被当成合法整数放行。"""
        self.assertTrue(VS.validate(True, {"type": "integer"}))

    def test_required_and_unknown_fields(self) -> None:
        schema = {"type": "object", "required": ["a"], "additionalProperties": False,
                  "properties": {"a": {"type": "string"}}}
        self.assertTrue(VS.validate({}, schema))                    # 缺 a
        self.assertTrue(VS.validate({"a": "x", "b": 1}, schema))    # 多出 b
        self.assertFalse(VS.validate({"a": "x"}, schema))

    def test_enum_pattern_minitems(self) -> None:
        self.assertTrue(VS.validate("z", {"type": "string", "enum": ["a"]}))
        self.assertTrue(VS.validate("2026/09/05", {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}))
        self.assertTrue(VS.validate([], {"type": "array", "minItems": 1}))

    def test_conditional_required(self) -> None:
        schema = json.loads((SYSTEM_ROOT / "schemas" / "front_matter.schema.json").read_text(encoding="utf-8"))
        base = {"type": "Spec", "topic": "某主题", "date": "2026-09-05",
                "author": "Builder", "status": "draft"}
        self.assertTrue(VS.validate(base, schema), "type: Spec 缺 id 应报错")
        self.assertFalse(VS.validate({**base, "id": "Tech-11"}, schema))

    def test_live_route_map_conforms(self) -> None:
        self.assertEqual(VS.check_route_map(), [])

    def test_missing_route_map_fails_closed(self) -> None:
        self.assertTrue(VS.check_route_map(Path("/nonexistent/route_map.json")))

    def test_audit_schema_selftest(self) -> None:
        self.assertEqual(VS.check_audit_report_schema_selftest(), [])

    def test_front_matter_parser(self) -> None:
        fm = VS.parse_front_matter("---\ntype: Research\naudit_max_rounds: 3\n---\n正文")
        self.assertEqual(fm["type"], "Research")
        self.assertEqual(fm["audit_max_rounds"], 3)
        self.assertIsNone(VS.parse_front_matter("没有 front matter"))

    def test_deliberately_inconsistent_markdown_fixture_reports_specific_field(self) -> None:
        """验收场景：规则正文改了但 Schema 示例未同步改——喂一份实拍 .md 文件，
        必须非零结果并指出具体字段，不能只笼统说"校验失败"。"""
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "20260908_主题" / "02_方案.md"
            bad.parent.mkdir(parents=True)
            bad.write_text(
                "---\ntype: Spec\ntopic: 测试\ndate: 2026-09-08\nauthor: Builder\n"
                "status: draft\n---\n正文\n",
                encoding="utf-8",
            )  # type: Spec 但漏了 conditional_required 的 id 字段
            errs = VS.check_markdown(bad)
        self.assertTrue(errs)
        self.assertTrue(any("id" in e for e in errs), errs)


class AuditGateFailClosedTests(TestCase):
    CLOSED = "## 问题 1\n级别: Critical\n状态: 已关闭\n"

    def test_missing_independence_now_blocks(self) -> None:
        """缺 independence 曾经直接放行——漏报比如实上报更容易过关。"""
        text = "---\ntype: Audit\n---\n" + self.CLOSED
        self.assertTrue(GATE.check_report(text))

    def test_downgraded_still_blocks(self) -> None:
        text = ("---\nindependence: session-internal-downgraded (external: some-cli missing)\n---\n"
                + self.CLOSED)
        self.assertTrue(GATE.check_report(text))

    def test_external_reviewer_passes(self) -> None:
        text = "---\nindependence: external: some-cli\n---\n" + self.CLOSED
        self.assertEqual(GATE.check_report(text), [])

    def test_no_closed_critical_passes_without_field(self) -> None:
        """没有已关闭的 Critical 时不强求 independence，避免门禁扩权到无关报告。"""
        text = "---\ntype: Audit\n---\n级别: Critical\n状态: 待处理\n"
        self.assertEqual(GATE.check_report(text), [])

    def test_fullwidth_colon_recognized(self) -> None:
        text = "---\ntype: Audit\n---\n级别：Critical\n状态：已关闭\n"
        self.assertTrue(GATE.check_report(text))


class DataProvenanceTests(TestCase):
    def test_stamp_json_and_md(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            j, m = d / "board_config.json", d / "registry.md"
            j.write_text('{"providers": []}', encoding="utf-8")
            m.write_text("# 标题\n正文\n", encoding="utf-8")

            self.assertFalse(PROV.has_provenance(j))
            self.assertTrue(PROV.stamp(j))
            self.assertTrue(PROV.has_provenance(j))
            meta = json.loads(j.read_text(encoding="utf-8"))
            self.assertEqual(meta["_meta"]["policy"], "merge-only")
            self.assertEqual(meta["providers"], [], "原有内容必须保留")

            self.assertTrue(PROV.stamp(m))
            self.assertTrue(m.read_text(encoding="utf-8").startswith("---\nsource:"))
            self.assertIn("# 标题", m.read_text(encoding="utf-8"))

    def test_stamp_is_idempotent(self) -> None:
        """工具自身必须遵守它声明的 merge-only：已有标记不得覆盖。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tips.md"
            p.write_text("---\nsource: 人工\npolicy: append-only\n---\n\n内容\n", encoding="utf-8")
            before = p.read_text(encoding="utf-8")
            self.assertFalse(PROV.stamp(p))
            self.assertEqual(p.read_text(encoding="utf-8"), before)

    def test_unknown_file_defaults_to_protected(self) -> None:
        meta = PROV._meta_for("某个没登记的文件.md")
        self.assertEqual(meta["policy"], "merge-only")

    def test_all_policies_declared_are_valid(self) -> None:
        for name, meta in PROV.KNOWN.items():
            self.assertIn(meta["policy"], PROV.VALID_POLICIES, name)

    def test_source_buckets_map_to_system(self) -> None:
        """三个桶的对应关系必须成立，否则「路径即来源指针」就是空话。"""
        sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
        import lint_workspace as LW
        self.assertEqual(LW.check_data_source_mapping(SYSTEM_ROOT.parent), [])

    def test_no_toplevel_instance_files(self) -> None:
        data = SYSTEM_ROOT.parent / ".data"
        stray = [p.name for p in data.glob("*") if p.is_file() and p.suffix in (".md", ".json")]
        self.assertEqual(stray, [], f"顶层散落实例文件未归桶: {stray}")

    def test_credentials_dir_never_scanned(self) -> None:
        names = [p.name for p in PROV.target_files()]
        self.assertNotIn("credentials", names)
        self.assertTrue(all(not p.is_dir() for p in PROV.target_files()))


if __name__ == "__main__":
    import unittest

    unittest.main()
