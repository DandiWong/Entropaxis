"""调度回执测试（审计 C-03）。

此前承载信息盖在交付物 Front Matter 里，而 Front Matter 与模型同权限可编辑——伪造
`carrier: forged-profile` 即可让门禁放行，「第三方可核验」并不成立。本组证明信任根
已移出模型可写区：回执在工作目录之外、由本机密钥签名、绑定交付物内容。
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

import dispatch_receipt as rc  # noqa: E402


class DispatchReceiptTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self._old = (rc.RECEIPT_DIR, rc.KEY_FILE)
        rc.RECEIPT_DIR = self.ws / "receipts"
        rc.KEY_FILE = self.ws / "credentials" / "k.key"
        self.deliverable = self.ws / "05.md"
        self.deliverable.write_text("# 报告\n内容\n", encoding="utf-8")

    def tearDown(self) -> None:
        rc.RECEIPT_DIR, rc.KEY_FILE = self._old
        self._tmp.cleanup()

    def _issue(self):
        return rc.issue("Reviewer", "a" * 64, self.deliverable, None, "reviewer-primary", "omp -p {PROMPT}")

    def test_issue_then_verify(self) -> None:
        r = self._issue()
        self.assertTrue(r["receipt_id"].startswith("rcpt-"))
        self.assertNotEqual(r["mac"], rc.UNSIGNED)
        ok, why = rc.verify(r["receipt_id"], self.deliverable)
        self.assertTrue(ok, why)

    def test_receipt_written_under_its_own_dir(self) -> None:
        r = self._issue()
        self.assertTrue((rc.RECEIPT_DIR / f"{r['receipt_id']}.json").is_file())

    def test_production_paths_live_outside_any_capsule(self) -> None:
        """信任根必须在被调度角色的 --cwd 够不着的地方——对生产常量断言，不是测试替身。"""
        real_dir, real_key = rc.DEFAULT_RECEIPT_DIR, self._old[1]
        data = SYSTEM_ROOT / "data"
        for p in (real_dir, real_key):
            self.assertTrue(str(p).startswith(str(data)), f"{p} 应在 .entropaxis/data/ 下")
            self.assertNotIn("docs", p.parts, "回执与密钥不得落在胶囊所在的 docs/ 树内")

    def test_edited_deliverable_breaks_the_binding(self) -> None:
        """回执证明的是「这一份内容由这条命令产出」，不是「某次调度发生过」。"""
        r = self._issue()
        self.deliverable.write_text("# 报告\n被改写过\n", encoding="utf-8")
        ok, why = rc.verify(r["receipt_id"], self.deliverable)
        self.assertFalse(ok)
        self.assertIn("不符", why)

    def test_forged_receipt_file_fails_mac(self) -> None:
        """伪造回执文件也没用：MAC 由本机密钥生成，密钥不在版本库里。"""
        r = self._issue()
        f = rc.RECEIPT_DIR / f"{r['receipt_id']}.json"
        data = json.loads(f.read_text(encoding="utf-8"))
        data["carrier"] = "forged-profile"
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        ok, why = rc.verify(r["receipt_id"], self.deliverable)
        self.assertFalse(ok)
        self.assertIn("MAC", why)

    def test_nonexistent_receipt_rejected(self) -> None:
        ok, why = rc.verify("rcpt-" + "0" * 16)
        self.assertFalse(ok)
        self.assertIn("不存在", why)

    def test_unsigned_is_not_verified(self) -> None:
        """密钥缺失时签发降级为不签，但绝不把未签名当作已验证。"""
        r = self._issue()
        rc.KEY_FILE.unlink()
        ok, why = rc.verify(r["receipt_id"], self.deliverable)
        self.assertFalse(ok)
        self.assertIn("未签名", why)

    def test_receipt_role_must_match(self) -> None:
        """一份 role: Architecture 的**有效**回执曾可充当 Reviewer 的独立性证据（复核 C-03）。"""
        r = rc.issue("Architecture", "b" * 64, self.deliverable, None, "architecture-primary")
        ok, why = rc.verify(r["receipt_id"], carrier="architecture-primary", role="Reviewer")
        self.assertFalse(ok)
        self.assertIn("不能充当", why)
        ok, _ = rc.verify(r["receipt_id"], carrier="architecture-primary", role="Architecture")
        self.assertTrue(ok)

    def test_key_is_owner_only(self) -> None:
        self._issue()
        self.assertEqual(rc.KEY_FILE.stat().st_mode & 0o077, 0, "密钥不得对同组/其他用户可读")

    def test_body_hash_survives_front_matter_stamping(self) -> None:
        """签发在盖章之前；绑定整文件哈希则回执与落盘文件必然不等，核验永远失配。"""
        doc = self.ws / "d.md"
        doc.write_text("---\ntype: Report\ncarrier: session-local\n---\n\n# 正文\n内容\n", encoding="utf-8")
        before = rc.body_sha256(doc)
        doc.write_text("---\ntype: Report\ncarrier: reviewer-primary\nreceipt_id: rcpt-0\n---\n\n# 正文\n内容\n", encoding="utf-8")
        self.assertEqual(before, rc.body_sha256(doc), "改 Front Matter 不应改变正文指纹")
        doc.write_text("---\ntype: Report\ncarrier: reviewer-primary\n---\n\n# 正文\n被篡改\n", encoding="utf-8")
        self.assertNotEqual(before, rc.body_sha256(doc), "改正文必须改变指纹")

    def test_carrier_mismatch_rejected(self) -> None:
        """挂着真回执改一行 carrier 冒充另一个承载——本轮实测的残留旁路。"""
        r = self._issue()
        ok, why = rc.verify(r["receipt_id"], carrier="forged-profile")
        self.assertFalse(ok)
        self.assertIn("被改写", why)
        ok, _ = rc.verify(r["receipt_id"], carrier="reviewer-primary")
        self.assertTrue(ok)

    def test_target_mismatch_rejected(self) -> None:
        """回执再有效，绑的若是另一个受审对象，就不能证明复核的是报告声明的那一版（复核 C-03）。"""
        target = self.ws / "02_方案.md"
        target.write_text("# 方案\n", encoding="utf-8")
        r = rc.issue("Reviewer", "a" * 64, self.deliverable, target, "reviewer-primary")
        ok, why = rc.verify(r["receipt_id"], target_sha256="b" * 64)
        self.assertFalse(ok)
        self.assertIn("受审对象", why)
        ok, why = rc.verify(r["receipt_id"], target_sha256=r["target_sha256"])
        self.assertTrue(ok, why)

    def test_directory_deliverable_is_content_bound(self) -> None:
        """目录型交付物此前签发端落空字符串、核验端只看文件——回执只证明"签发过"，
        改了目录里的文件照样通过（复核 M-08）。"""
        d = self.ws / "08_汇报"
        d.mkdir()
        (d / "deck.html").write_text("<html>v1</html>", encoding="utf-8")
        r = rc.issue("Reporter", "a" * 64, d, None, "reporter-primary")
        self.assertTrue(r["deliverable_sha256"], "目录必须有内容指纹，不得留空")
        ok, why = rc.verify(r["receipt_id"], d)
        self.assertTrue(ok, why)
        (d / "deck.html").write_text("<html>被改写</html>", encoding="utf-8")
        ok, why = rc.verify(r["receipt_id"], d)
        self.assertFalse(ok, "目录内容被改写后回执必须失配")
        self.assertIn("不符", why)

    def test_dir_digest_shared_with_dispatch_role(self) -> None:
        """档案指纹与回执指纹必须同算法：各留一份实现只会漂移。"""
        sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
        import dispatch_role as dr  # noqa: PLC0415
        d = self.ws / "out"
        d.mkdir()
        (d / "a.txt").write_text("x", encoding="utf-8")
        self.assertEqual(dr._sha256_path(d), rc.content_sha256(d))

    def test_unbound_target_cannot_back_a_declared_one(self) -> None:
        """调度时没 --target 的回执，不得为一份声明了 target_sha256 的报告背书。"""
        r = self._issue()  # target=None → 回执 target_sha256 为空
        ok, why = rc.verify(r["receipt_id"], target_sha256="c" * 64)
        self.assertFalse(ok)
        self.assertIn("未绑定", why)


class ReceiptOrderingTests(TestCase):
    """签发顺序：回执待判据通过后才签发，判据校验时不得反过来要求 receipt_id。"""

    def test_dispatch_time_check_skips_receipt_requirement(self) -> None:
        sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
        import check_audit_gate as gate  # noqa: PLC0415
        text = ("---\ntype: Audit\ntopic: t\ndate: 2026-09-19\nauthor: Reviewer\nstatus: active\n"
                "schema_version: 3\nreviewer_mode: external\nreviewer_ref: omp -p x\n"
                "carrier: reviewer-primary\ntarget_path: 01.md\ntarget_sha256: " + "a" * 64 + "\n---\n\n"
                '# 审计\n\n```audit-state\n{"issues": [], "critical_acks": []}\n```\n')
        self.assertTrue(any("receipt_id" in i for i in gate.check_report(text)),
                        "落盘后的复核必须要求回执")
        self.assertFalse([i for i in gate.check_report(text, receipt_check=False) if "receipt_id" in i],
                         "签发前的判据校验不得要求尚不存在的回执")
