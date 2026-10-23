"""内容证据阶梯与聚类的回归测试：三级读取、损坏文件、虚词过滤、簇归并。"""
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

from tools import content_evidence


class ContentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make_docx(self, path: Path, text: str) -> Path:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", f"<w:document><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:document>")
        return path

    def test_tokenize_drops_particles_and_keeps_terms(self):
        tokens = content_evidence.tokenize("的项目专利申请")
        self.assertNotIn("的项", tokens)
        self.assertIn("专利", tokens)
        self.assertIn("申请", tokens)

    def test_tier1_uses_names_only(self):
        directory = self.root / "专利资料"
        directory.mkdir()
        (directory / "说明.md").write_text("完全不同的内容 XYZ", encoding="utf-8")
        evidence = content_evidence.DirEvidence(directory, self.root)
        self.assertIn("专利", evidence.tokens(1))
        self.assertNotIn("完全", evidence.tokens(1))
        self.assertIn("完全", evidence.tokens(2))

    def test_tier3_reads_docx_and_pdf(self):
        directory = self.root / "materials"
        directory.mkdir()
        self.make_docx(directory / "方案.docx", "注册流程规范")
        evidence = content_evidence.DirEvidence(directory, self.root)
        self.assertIn("注册", evidence.tokens(3))
        self.assertTrue(any("pdf" in e for e in evidence.errors) is False)

    def test_broken_docx_and_pdf_raise_value_error(self):
        broken = self.root / "broken.docx"
        broken.write_text("not a zip", encoding="utf-8")
        with self.assertRaises(ValueError):
            content_evidence.read_docx_text(broken)
        empty_pdf = self.root / "empty.pdf"
        empty_pdf.write_bytes(b"%PDF-1.4 no streams here")
        with self.assertRaises(ValueError):
            content_evidence.read_pdf_text(empty_pdf)

    def test_broken_binary_is_recorded_not_raised(self):
        directory = self.root / "reports"
        directory.mkdir()
        (directory / "bad.pdf").write_bytes(b"%PDF-1.4 broken")
        evidence = content_evidence.DirEvidence(directory, self.root)
        evidence.tokens(3)
        self.assertTrue(evidence.errors, "tier3 解析失败须记入 errors，不得静默丢弃")
        self.assertIn("bad.pdf", "; ".join(evidence.errors))

    # ---- M-2/M-3：边界跳过与有界读取 ----

    def test_skip_predicate_prunes_boundary_material(self):
        """M-2：显式排除/归属边界内的文件不进入证据，也不进名称 token。"""
        directory = self.root / "alpha"
        (directory / "private").mkdir(parents=True)
        (directory / "private" / "notes.txt").write_text("机密并购材料", encoding="utf-8")
        (directory / "公开.md").write_text("公开说明", encoding="utf-8")
        evidence = content_evidence.DirEvidence(
            directory, self.root,
            skip=lambda entry: "private" in entry.relative_to(self.root).parts)
        self.assertEqual([p.name for p in evidence.files], ["公开.md"])
        self.assertEqual(evidence.skipped_by_boundary, 1)
        self.assertNotIn("机密", evidence.tokens(2), "排除子树内容不得成为任何层级的证据")

    def test_docx_input_rejected_before_read_when_over_limit(self):
        """M-3：超限输入在读取前拒绝（stat 先行），不做解压分配。"""
        big = self.root / "big.docx"
        big.write_bytes(b"0" * 64)
        with mock.patch.object(content_evidence, "MAX_BINARY_BYTES", 32):
            with self.assertRaises(ValueError) as ctx:
                content_evidence.read_docx_text(big)
        self.assertIn("超出读取上限", str(ctx.exception))

    def test_docx_member_decompression_is_bounded(self):
        """M-3：压缩包很小但解压成员超限时拒绝（zip 炸弹反例）。"""
        bomb = self.root / "bomb.docx"
        payload = b"<w:t>" + b"alpha evidence " * 2048 + b"</w:t>"
        with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", payload)
        self.assertLess(bomb.stat().st_size, 4096, "夹具须是小输入大输出")
        with mock.patch.object(content_evidence, "MAX_BINARY_BYTES", 4096):
            with self.assertRaises(ValueError) as ctx:
                content_evidence.read_docx_text(bomb)
        self.assertIn("超出读取上限", str(ctx.exception))

    def test_pdf_input_rejected_before_read_when_over_limit(self):
        fat = self.root / "fat.pdf"
        fat.write_bytes(b"%PDF-1.4 " + b"0" * 64)
        with mock.patch.object(content_evidence, "MAX_BINARY_BYTES", 32):
            with self.assertRaises(ValueError) as ctx:
                content_evidence.read_pdf_text(fat)
        self.assertIn("超出读取上限", str(ctx.exception))

    def test_pdf_stream_decompression_is_bounded(self):
        """M-3：PDF 内容流解压输出有界，超限即拒绝。"""
        tiny = self.root / "tiny.pdf"
        stream = zlib.compress(b"(" + b"beta evidence " * 2048 + b")")
        tiny.write_bytes(b"%PDF-1.4\nstream\n" + stream + b"\nendstream\n")
        self.assertLess(tiny.stat().st_size, 4096, "夹具须是小输入大输出")
        with mock.patch.object(content_evidence, "MAX_BINARY_BYTES", 4096):
            with self.assertRaises(ValueError) as ctx:
                content_evidence.read_pdf_text(tiny)
        self.assertIn("超出上限", str(ctx.exception))

    def test_related_needs_three_shared_tokens(self):
        left = self.root / "专利资料"
        right = self.root / "商标资料"
        left.mkdir()
        right.mkdir()
        (left / "说明.md").write_text("专利申请流程与规范", encoding="utf-8")
        (right / "说明.md").write_text("专利申请流程与规范", encoding="utf-8")
        a = content_evidence.DirEvidence(left, self.root)
        b = content_evidence.DirEvidence(right, self.root)
        hit = content_evidence.related(a, b)
        self.assertIsNotNone(hit)
        self.assertGreaterEqual(hit["tier"], 2)
        self.assertIsNone(content_evidence.related(a, b, threshold=0.99, min_shared=99))

    def test_cluster_groups_only_related_directories(self):
        for name, text in (("专利资料", "专利申请流程"), ("商标资料", "专利申请流程"), ("食堂", "今日菜单")):
            directory = self.root / name
            directory.mkdir()
            (directory / "说明.md").write_text(text, encoding="utf-8")
        evidences = [content_evidence.DirEvidence(self.root / n, self.root)
                     for n in ("专利资料", "商标资料", "食堂")]
        groups = content_evidence.cluster(evidences)
        sizes = sorted(len(g) for g in groups)
        self.assertEqual(sizes, [1, 2])


if __name__ == "__main__":
    unittest.main()
