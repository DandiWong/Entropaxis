import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import lint_workspace as lw
from tools import update_audit_state as uas
from tools import update_capsule as uc
from tests.test_update_audit_state import REPORT_TEMPLATE

TOOL = Path(uc.__file__)

MANIFEST = """# 事务胶囊清单
id: Tech-1
lifecycle: {lifecycle}
delivered_at: null        # 交付日
review_due_at: null       # +30 天
closure_deadline: null    # +60 天
relations: []
"""

ACCEPTANCE = """# 验收报告
- **数据回收日期**：{data}
- **未度量兜底说明**：{unmeasured}
"""


class UpdateCapsuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.cap = Path(self._td.name) / "20261006_主题"
        self.cap.mkdir()
        self.manifest(lifecycle="draft")

    def tearDown(self) -> None:
        self._td.cleanup()

    def manifest(self, **kw) -> None:
        (self.cap / "capsule.yaml").write_text(MANIFEST.format(**kw), encoding="utf-8")

    def report(self, issues: str) -> Path:
        target = self.cap / "02_方案.md"
        target.write_text("方案\n", encoding="utf-8")
        sha = hashlib.sha256(target.read_bytes()).hexdigest()
        text = REPORT_TEMPLATE.format(mode="external", target=target.name, sha=sha, extra="")
        start, end = text.index('"issues": [') + len('"issues": ['), text.index("],\n  \"critical_acks\"")
        path = self.cap / "05_审计报告.md"
        path.write_text(text[:start] + issues + text[end:], encoding="utf-8")
        return path

    def lifecycle(self) -> str:
        return uc._read_key((self.cap / "capsule.yaml").read_text(encoding="utf-8"), "lifecycle")

    def test_open_major_keeps_draft_then_close_via_audit_state_advances(self) -> None:
        report = self.report('{"id": "M-1", "level": "Major", "status": "open"}')
        self.assertEqual(uc.update_capsule(str(self.cap))["to"], "draft")
        text = report.read_text(encoding="utf-8")
        uas.commit(report, uas.dump_state(text, uas.set_field(uas.load_state(text), "M-1", "status", "closed")))
        self.assertEqual(self.lifecycle(), "active", "关闭最后一个阻断问题应由 update_audit_state 顺带推进胶囊")

    def test_open_minor_does_not_block(self) -> None:
        self.report('{"id": "m-1", "level": "Minor", "status": "open"}')
        self.assertEqual(uc.update_capsule(str(self.cap))["to"], "active")

    def test_spec_advances_and_comments_preserved(self) -> None:
        (self.cap / "04_Spec_Task-1.md").write_text("x", encoding="utf-8")
        uc.update_capsule(str(self.cap))
        text = (self.cap / "capsule.yaml").read_text(encoding="utf-8")
        self.assertIn("lifecycle: active\n", text)
        self.assertIn("delivered_at: null        # 交付日", text)

    def test_delivered_derives_dates_and_is_write_once(self) -> None:
        self.manifest(lifecycle="active")
        res = uc.update_capsule(str(self.cap), delivered="2026-10-06")
        self.assertEqual(res["changes"], {"delivered_at": "2026-10-06", "review_due_at": "2026-11-05",
                                          "closure_deadline": "2026-12-05", "lifecycle": "delivered"})
        self.assertIn("review_due_at: 2026-11-05       # +30 天",
                      (self.cap / "capsule.yaml").read_text(encoding="utf-8"))
        with self.assertRaises(uc.ToolError):
            uc.update_capsule(str(self.cap), delivered="2026-10-07")

    def test_bad_date_rejected(self) -> None:
        with self.assertRaises(uc.ToolError):
            uc.update_capsule(str(self.cap), delivered="10/06")

    def test_archive_from_acceptance(self) -> None:
        for data, unmeasured, expected in (("2026-11-05", "待回填", "archived"),
                                           ("待回填", "业务方未提供数据", "archived-unmeasured"),
                                           ("待回填", "待回填", "delivered")):
            self.manifest(lifecycle="delivered")
            (self.cap / "07_验收报告.md").write_text(ACCEPTANCE.format(data=data, unmeasured=unmeasured), encoding="utf-8")
            self.assertEqual(uc.update_capsule(str(self.cap))["to"], expected)

    def test_never_moves_backwards_or_out_of_terminal(self) -> None:
        self.manifest(lifecycle="archived")
        (self.cap / "04_Spec_Task-1.md").write_text("x", encoding="utf-8")
        self.assertEqual(uc.update_capsule(str(self.cap))["changes"], {})

    def test_dry_run_writes_nothing(self) -> None:
        (self.cap / "04_Spec_Task-1.md").write_text("x", encoding="utf-8")
        self.assertEqual(uc.update_capsule(str(self.cap), dry_run=True)["to"], "active")
        self.assertEqual(self.lifecycle(), "draft")

    def test_sync_for_outside_capsule_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(uc.sync_for(Path(td) / "x.md"))

    def test_lint_reports_drift(self) -> None:
        root = Path(self._td.name)
        (self.cap / "04_Spec_Task-1.md").write_text("x", encoding="utf-8")
        tools = root / ".entropaxis" / "tools"
        tools.mkdir(parents=True)
        (tools / "update_capsule.py").write_text(TOOL.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertTrue(any("[胶囊状态落后]" in i for i in lw.check_capsule_lifecycle(root)))
        uc.update_capsule(str(self.cap))
        self.assertEqual(lw.check_capsule_lifecycle(root), [])

    def test_cli(self) -> None:
        ok = subprocess.run([sys.executable, str(TOOL), str(self.cap), "--json"], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["to"], "draft")
        bad = subprocess.run([sys.executable, str(TOOL), str(self.cap / "nope")], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("👉", bad.stderr)


if __name__ == "__main__":
    unittest.main()
