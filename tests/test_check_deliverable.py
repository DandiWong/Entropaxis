import unittest
import zipfile
import zlib
from pathlib import Path
import tempfile

from tools.check_deliverable import check_file, main


def _office(path: Path, suffix: str, body: str, *, omit_body: bool = False, bad_xml: bool = False) -> None:
    required = {
        ".docx": "word/document.xml",
        ".pptx": "ppt/presentation.xml",
        ".xlsx": "xl/workbook.xml",
    }[suffix]
    xml = "<root>" if bad_xml else f"<root>{body}</root>"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types></Types>")
        if not omit_body:
            archive.writestr(required, xml)


def _pdf(path: Path, extra: bytes = b"", *, eof: bool = True, startxref: bool = True) -> None:
    tail = b""
    if startxref:
        tail += b"startxref\n0\n"
    if eof:
        tail += b"%%EOF\n"
    path.write_bytes(b"%PDF-1.4\n" + extra + tail)


class CheckDeliverableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_valid_office_and_pdf_pass(self):
        docx = self.root / "a.docx"
        pdf = self.root / "a.pdf"
        _office(docx, ".docx", "正式内容")
        _pdf(pdf)
        self.assertEqual(check_file(docx), [])
        self.assertEqual(check_file(pdf), [])

    def test_broken_zip_missing_part_and_bad_xml(self):
        broken = self.root / "bad.docx"
        broken.write_bytes(b"not a zip")
        self.assertIn("不是有效的 Office 压缩包", check_file(broken)[0])
        empty = self.root / "empty.pptx"
        _office(empty, ".pptx", "", omit_body=True)
        self.assertTrue(any("缺少正文部件" in item for item in check_file(empty)))
        bad = self.root / "bad.xlsx"
        _office(bad, ".xlsx", "", bad_xml=True)
        self.assertTrue(any("不是合法 XML" in item for item in check_file(bad)))

    def test_placeholders_in_office_and_compressed_pdf(self):
        docx = self.root / "ph.docx"
        _office(docx, ".docx", "这里待回填，还有{{主题}}")
        issues = check_file(docx)
        self.assertEqual(len(issues), 1)
        self.assertIn("待回填", issues[0])
    def test_exit_code(self):
        bad = self.root / "bad.docx"
        bad.write_bytes(b"nope")
        self.assertEqual(main([str(bad)]), 1)
    def test_truncated_pdf_and_unsupported_suffix(self):
        pdf = self.root / "cut.pdf"
        _pdf(pdf, eof=False)
        self.assertIn("%%EOF", check_file(pdf)[0])
        other = self.root / "note.md"
        other.write_text("x", encoding="utf-8")
        self.assertIn("不支持的后缀", check_file(other)[0])
        missing = self.root / "gone.docx"
        self.assertIn("文件不存在", check_file(missing)[0])



class CliOutputTests(unittest.TestCase):
    def test_ok_output(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.docx"
            _office(path, ".docx", "好")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main([str(path)])
            self.assertEqual(code, 0)
            self.assertEqual(buf.getvalue().strip(), "ok")
