#!/usr/bin/env python3
"""交付物可解析性检查：Office 压缩包与 PDF 结构，以及占位符残留。

不检查排版、字阶或事实是否与上下文一致（《文件交付》第 3 节第 2、3 条）。
PDF 只核结构与可inflate的文本流，不渲染页面。
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

OFFICE = {
    ".docx": "word/document.xml",
    ".pptx": "ppt/presentation.xml",
    ".xlsx": "xl/workbook.xml",
}
PDF_SUFFIX = ".pdf"
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_STREAM_BYTES = 8 * 1024 * 1024
PLACEHOLDERS = ("{{", "待回填")


class ToolError(Exception):
    """调用方式错误，不是单个文件的检查结论。"""


def _issue(reason: str, fix: str) -> str:
    return f"{reason}。👉 {fix}"


def _xml_text(raw: bytes) -> str:
    root = ET.fromstring(raw)
    return "".join(root.itertext())


def _placeholders(text: str) -> list[str]:
    return [token for token in PLACEHOLDERS if token in text]


def _check_office(path: Path, suffix: str) -> list[str]:
    required = OFFICE[suffix]
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return [_issue("不是有效的 Office 压缩包", "重新导出该文件，不要把损坏的半成品交付")]
    issues = []
    try:
        names = set(archive.namelist())
        if "[Content_Types].xml" not in names:
            issues.append(_issue("缺少 [Content_Types].xml", "用正源重新导出，不要手改压缩包"))
        if required not in names:
            issues.append(_issue(f"缺少正文部件 {required}", "确认导出的是该格式的完整文件，而不是空壳或改了后缀"))
        for name in sorted(names):
            if not name.endswith(".xml"):
                continue
            info = archive.getinfo(name)
            if info.file_size > MAX_XML_BYTES:
                issues.append(_issue(f"{name} 超过 {MAX_XML_BYTES // (1024 * 1024)}MB", "拆分或压缩后再检查占位符"))
                continue
            raw = archive.read(name)
            try:
                text = _xml_text(raw)
            except ET.ParseError as exc:
                issues.append(_issue(f"{name} 不是合法 XML（{exc}）", "重新导出，不要保留未闭合的部件"))
                continue
            found = _placeholders(text)
            if found:
                issues.append(_issue(f"{name} 残留占位符 {'、'.join(found)}", "替换为正式内容后再交付"))
    finally:
        archive.close()
    return issues


def _pdf_streams(data: bytes) -> list[bytes]:
    """取出 FlateDecode 流并解压。解压失败视为损坏。"""
    texts = []
    cursor = 0
    while True:
        start = data.find(b"stream", cursor)
        if start < 0:
            break
        dict_start = data.rfind(b"<<", max(0, start - 4096), start)
        header = data[dict_start:start] if dict_start >= 0 else b""
        payload_at = start + len(b"stream")
        if payload_at < len(data) and data[payload_at:payload_at + 1] in (b"\n", b"\r"):
            payload_at += 2 if data[payload_at:payload_at + 2] == b"\r\n" else 1
        end = data.find(b"endstream", payload_at)
        if end < 0:
            raise ValueError("PDF 流没有 endstream")
        blob = data[payload_at:end].rstrip(b"\r\n")
        cursor = end + len(b"endstream")
        if b"FlateDecode" not in header:
            texts.append(blob)
            continue
        if len(blob) > MAX_STREAM_BYTES:
            raise ValueError("PDF 流超过检查上限")
        try:
            texts.append(zlib.decompress(blob))
        except zlib.error as exc:
            raise ValueError(f"PDF 流无法解压（{exc}）") from exc
    return texts


def _check_pdf(path: Path) -> list[str]:
    data = path.read_bytes()
    if not data.startswith(b"%PDF-"):
        return [_issue("文件头不是 %PDF-", "重新导出 PDF，不要改后缀冒充")]
    if b"%%EOF" not in data[-2048:] and b"%%EOF" not in data:
        return [_issue("缺少 %%EOF，文件可能被截断", "重新导出完整 PDF")]
    if b"startxref" not in data:
        return [_issue("缺少 startxref", "重新导出完整 PDF")]
    try:
        chunks = _pdf_streams(data)
    except ValueError as exc:
        return [_issue(str(exc), "重新导出 PDF，不要交付写到一半的文件")]
    text = data.decode("latin-1", errors="ignore")
    for chunk in chunks:
        text += "\n" + chunk.decode("utf-8", errors="ignore")
    found = _placeholders(text)
    if found:
        return [_issue(f"残留占位符 {'、'.join(found)}", "替换为正式内容后再交付")]
    return []


def check_file(path: Path) -> list[str]:
    if not path.is_file():
        return [_issue(f"文件不存在: {path}", "传入已落盘的交付物路径")]
    suffix = path.suffix.lower()
    if suffix in OFFICE:
        return _check_office(path, suffix)
    if suffix == PDF_SUFFIX:
        return _check_pdf(path)
    return [_issue(f"不支持的后缀 {suffix or '(无)'}", "只传入 .docx、.pptx、.xlsx 或 .pdf")]


def check_paths(paths: list[Path]) -> dict:
    files = []
    for path in paths:
        issues = check_file(path)
        files.append({"path": str(path), "ok": not issues, "issues": issues})
    return {"ok": all(item["ok"] for item in files), "files": files}


def _render(result: dict) -> str:
    if result["ok"]:
        return "ok"
    lines = []
    for item in result["files"]:
        for issue in item["issues"]:
            lines.append(f"❌ {item['path']}: {issue}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 Office/PDF 能否解析，以及是否残留占位符")
    parser.add_argument("paths", nargs="+", type=Path, help="一个或多个 .docx/.pptx/.xlsx/.pdf")
    args = parser.parse_args(argv)
    result = check_paths(args.paths)
    print(_render(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
