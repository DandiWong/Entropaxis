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

class CapsuleSecondsContractTests(TestCase):
    """秒级工时契约（Tech-176 T1，方案 4.3）：worklogs/segments/attempts/effort 的新字段与放宽。"""

    def _capsule_schema(self) -> dict:
        return VS.load_schema("capsule")

    def _p7_sample(self) -> dict:
        """Tech-175 capsule.yaml 2026-10-29 实测样本（节选，字段值原样）。"""
        return {
            "id": "Tech-175", "topic": "Entropaxis团队协作工作台", "mode": "full",
            "lifecycle": "draft", "seq_next": 7,
            "worklogs": [
                {"date": "2026-10-29", "stage": "07_验收报告", "spent": 0.25,
                 "summary": "方案·Architecture（自动计量入账）", "operator": "Dandi Wong", "ref": "Tech-175#1"},
                {"date": "2026-10-29", "stage": "03_设计", "spent": 0.5,
                 "summary": "设计稿R5·Designer（自动计量入账）", "operator": "Dandi Wong",
                 "ref": "Tech-175#5", "overridden": True, "original_spent": 0.25,
                 "original_summary": "设计稿R5·Designer（自动计量入账）"},
            ],
            "task_segments": [
                {"seq": 1, "seg_id": "Tech-175#1", "name": "方案·Architecture",
                 "startAt": "2026-10-29T17:40:00.102471+08:00",
                 "lastActivityAt": "2026-10-29T17:51:17.047585+08:00",
                 "state": "logged", "taskId": "Tech-175", "stage": "07_验收报告",
                 "endAt": "2026-10-29T17:51:17.047585+08:00", "trigger": "dispatch"},
                {"seq": 5, "seg_id": "Tech-175#5", "name": "设计稿R5·Designer",
                 "startAt": "2026-10-29T20:50:23.547769+08:00",
                 "lastActivityAt": "2026-10-29T20:52:04.942442+08:00",
                 "state": "logged", "taskId": "Tech-175", "stage": "03_设计",
                 "endAt": "2026-10-29T20:52:04.942442+08:00", "trigger": "dispatch"},
            ],
        }

    def test_p7_dispatch_trigger_sample_validates(self) -> None:
        """P-7 复现：实测样本含 trigger: dispatch，新契约下整体通过。"""
        capsule = self._p7_sample()
        errs = VS.validate(capsule, self._capsule_schema())
        self.assertEqual(errs, [], f"含 trigger: dispatch 的实测样本应整体通过: {errs}")
        broken = dict(capsule)
        broken["task_segments"] = [dict(capsule["task_segments"][0], trigger="bogus")]
        self.assertTrue(any("trigger" in e for e in VS.validate(broken, self._capsule_schema())))

    def test_spent_allows_any_fraction_but_not_zero(self) -> None:
        """spent 删除 multipleOf 0.25（方案 4.3.1）：0.3 通过，0 仍被拒。"""
        items = self._capsule_schema()["properties"]["worklogs"]["items"]
        base = {"date": "2026-10-30", "stage": "misc", "spent": 0.3,
                "summary": "七分钟按秒派生", "operator": "t", "ref": "Tech-1#1"}
        self.assertEqual(VS.validate(base, items), [])
        zero = dict(base, spent=0)
        self.assertTrue(any("未超过排他下限" in e for e in VS.validate(zero, items)))

    def test_worklog_seconds_actor_role(self) -> None:
        items = self._capsule_schema()["properties"]["worklogs"]["items"]
        entry = {"date": "2026-10-30", "stage": "04_实施", "spent": 0.1167, "seconds": 420,
                "actor": "role", "role": "Builder", "summary": "秒级条目",
                "operator": "t", "ref": "Tech-1#2"}
        self.assertEqual(VS.validate(entry, items), [])
        self.assertTrue(VS.validate(dict(entry, seconds=1.5), items), "seconds 须为整数")
        self.assertTrue(VS.validate(dict(entry, seconds=0), items), "seconds 须 >= 1")
        self.assertTrue(VS.validate(dict(entry, actor="alien"), items), "actor 枚举")
        self.assertTrue(VS.validate(dict(entry, role="Plumber"), items), "role 枚举")
        self.assertTrue(VS.validate(dict(entry, override_reason=""), items), "override_reason 非空")

    def test_segment_actor_dispatch_and_failed_state(self) -> None:
        items = self._capsule_schema()["properties"]["task_segments"]["items"]
        seg = {"seq": 1, "seg_id": "Tech-1#3", "name": "Builder→04_Spec_Tech-176.md",
               "startAt": "2026-10-30T10:00:00+08:00", "lastActivityAt": "2026-10-30T10:07:00+08:00",
               "state": "failed", "endAt": "2026-10-30T10:07:00+08:00", "trigger": "dispatch",
               "actor": "role", "role": "Builder",
               "dispatch_id": "0123456789abcdef0123456789abcdef",
               "machine_seconds": 420, "note": "CHAIN_EXHAUSTED"}
        self.assertEqual(VS.validate(seg, items), [])
        self.assertTrue(VS.validate(dict(seg, state="bogus"), items), "state 枚举")
        self.assertTrue(VS.validate(dict(seg, actor="human"), items), "段 actor 只认 role/manager")
        self.assertTrue(VS.validate(dict(seg, dispatch_id="xyz"), items), "dispatch_id 32 位 hex")

    def test_segment_manager_fields(self) -> None:
        items = self._capsule_schema()["properties"]["task_segments"]["items"]
        seg = {"seq": 2, "seg_id": "Tech-1#4", "name": "事务段", "stage": "misc",
               "startAt": "2026-10-30T11:00:00+08:00", "lastActivityAt": "2026-10-30T11:20:00+08:00",
               "state": "logged", "endAt": "2026-10-30T11:20:00+08:00", "trigger": "manager",
               "actor": "manager", "baseline": {"02_方案.md": "0123456789abcdef"},
               "evidence": "04_Spec_Tech-176.md", "evidence_sha256": "fedcba9876543210",
               "machine_seconds": 1200, "effective_seconds": 1200}
        self.assertEqual(VS.validate(seg, items), [])
        bad = dict(seg, baseline={"02_方案.md": "zz"})
        self.assertTrue(any("baseline" in e for e in VS.validate(bad, items)), "baseline 值须为 16 位 hex")

    def test_attempt_usage_and_dispatch_id(self) -> None:
        schema = VS.load_schema("roles_manifest")
        items = schema["properties"]["assignments"]["items"]["properties"]["attempts"]["items"]
        base = {"profile": "builder-1", "argv_sha256": "a" * 64,
                "started_at": "2026-10-30T10:00:00+08:00", "ended_at": "2026-10-30T10:07:00+08:00",
                "dispatch_id": "0123456789abcdef0123456789abcdef"}
        self.assertEqual(VS.validate({**base, "usage": "unknown"}, items), [])
        usage_obj = {"session_id": "s1", "models": {"gpt-x": {"in": 10, "out": 20, "cache_read": 0, "cache_write": 0}},
                     "cost_usd": 0.01}
        self.assertEqual(VS.validate({**base, "usage": usage_obj}, items), [])
        errs = VS.validate({**base, "usage": "n/a"}, items)
        self.assertTrue(errs, "usage 只认对象或 unknown")
        errs = VS.validate({**base, "usage": "unknown", "dispatch_id": "nothex"}, items)
        self.assertTrue(any("dispatch_id" in e for e in errs), "dispatch_id 32 位 hex")

    def test_effort_hms_pattern(self) -> None:
        """方案 4.3.4：effort 兼容旧 0.75h 与新 H:MM:SS。"""
        effort = VS.load_schema("front_matter")["properties"]["effort"]
        for ok in ("1:23:45", "0.75h", "1.5", "2h"):
            self.assertEqual(VS.validate(ok, effort), [], ok)
        for bad in ("1:83:45?", "1h23m"):
            self.assertTrue(VS.validate(bad, effort), bad)


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
            j, m = d / "sample.json", d / "registry.md"
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
        data = SYSTEM_ROOT / "data"
        stray = [p.name for p in data.glob("*") if p.is_file() and p.suffix in (".md", ".json")]
        self.assertEqual(stray, [], f"顶层散落实例文件未归桶: {stray}")

    def test_credentials_dir_never_scanned(self) -> None:
        names = [p.name for p in PROV.target_files()]
        self.assertNotIn("credentials", names)
        self.assertTrue(all(not p.is_dir() for p in PROV.target_files()))


if __name__ == "__main__":
    import unittest

    unittest.main()
