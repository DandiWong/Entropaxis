#!/usr/bin/env python3
"""将 patent-combo 的阶段底稿收敛为三份最终 Word 交付物。"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _is_section_heading(line: str, label: str) -> bool:
    title = re.sub(r"^#{1,6}\s*", "", line.strip())
    title = re.sub(r"^\d+(?:\.\d+)*[、.．]?\s*", "", title)
    return title == label



def _normalize_case_name(case_name: str) -> str:
    normalized = case_name.strip()
    if not normalized:
        raise ValueError("案件名称不能为空")
    if re.search(r'[\\/:*?"<>|\x00-\x1f]', normalized) or normalized in {".", ".."}:
        raise ValueError("案件名称含非法文件名字符")
    return normalized
def extract_report_section(markdown: str) -> str:
    """保留从“命中专利列表”到“人工检索式清单”之前的报告正文。"""
    lines = markdown.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if _is_section_heading(line, "命中专利列表")), None)
    if start is None:
        raise ValueError("查新报告缺少“命中专利列表”章节")
    end = next(
        (i for i, line in enumerate(lines[start + 1 :], start + 1) if _is_section_heading(line, "人工检索式清单")),
        None,
    )
    if end is None:
        raise ValueError("查新报告缺少“人工检索式清单”章节")
    if end <= start:
        raise ValueError("查新报告章节顺序无效")
    return "".join(lines[start:end]).rstrip() + "\n"


def _require_direct_child(case_dir: Path, path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.parent != case_dir.resolve() or not resolved.is_file():
        raise ValueError(f"{label}必须是案件目录内的现有 Markdown 文件: {path}")
    return resolved

def _is_invention(markdown: str) -> bool:
    return bool(re.search(r"\*\*专利类型\*\*\s*[：:]\s*发明", markdown))


def _validate_invention_disclosure(markdown: str) -> None:
    if not _is_invention(markdown):
        return
    mermaid_blocks = re.findall(r"```mermaid\s*\n.*?```", markdown, flags=re.DOTALL | re.IGNORECASE)
    if len(mermaid_blocks) < 2:
        raise ValueError("发明交底书必须包含至少两张 Mermaid 图：系统框图和系统流程图")
    code_section = re.search(
        r"^#{1,6}\s+.*关键实现代码.*?(?=^#{1,6}\s+|\Z)",
        markdown,
        flags=re.DOTALL | re.MULTILINE,
    )
    if not code_section or not re.search(
        r"```(?!mermaid\b)[^\n]*\n.+?```",
        code_section.group(0),
        flags=re.DOTALL | re.IGNORECASE,
    ):
        raise ValueError("发明交底书必须包含“关键实现代码”章节及至少一个脱敏代码或伪代码块")


def _render_mermaid(
    renderer: Path,
    source: Path,
    output: Path,
    runner: Runner,
) -> None:
    command = [
        sys.executable,
        str(renderer),
        "--input",
        str(source),
        "--output",
        str(output),
        "--no-docx",
    ]
    result = runner(command)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "未知渲染错误").strip()
        raise RuntimeError(f"Mermaid 出图失败（{source.name}）: {detail}")
    if not output.is_file():
        raise RuntimeError(f"Mermaid 出图未生成转换稿: {output}")
    rendered = output.read_text(encoding="utf-8")
    image_refs = re.findall(r"<!--\s*!\[[^\]]*\]\(([^)]+)\)\s*-->", rendered)
    if len(image_refs) < 2:
        raise RuntimeError("发明交底书未成功生成两张 Mermaid PNG，拒绝输出无图 Word")
    missing = [ref for ref in image_refs if not (output.parent / ref).is_file()]
    if missing:
        raise RuntimeError(f"Mermaid 图片文件缺失: {missing[0]}")



def _run_converter(
    converter: Path,
    source: Path,
    output: Path,
    base_dir: Path,
    runner: Runner,
) -> None:
    command = [
        sys.executable,
        str(converter),
        "--input",
        str(source),
        "--output",
        str(output),
        "--base-dir",
        str(base_dir),
    ]
    result = runner(command)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "未知转换错误").strip()
        raise RuntimeError(f"Word 导出失败（{source.name}）: {detail}")
    if not output.is_file():
        raise RuntimeError(f"Word 导出未生成目标文件: {output}")


def finalize(
    *,
    case_dir: Path,
    case_name: str,
    candidate_md: Path,
    disclosure_md: Path,
    claims_md: Path,
    report_md: Path,
    converter: Path,
    renderer: Path,
    runner: Runner = subprocess.run,
) -> list[Path]:
    case_name = _normalize_case_name(case_name)
    if not case_dir.is_dir():
        raise ValueError(f"案件目录不存在: {case_dir}")
    if not case_name.strip():
        raise ValueError("案件名称不能为空")
    if not converter.is_file():
        raise ValueError(f"Markdown 转 Word 工具不存在: {converter}")
    if not renderer.is_file():
        raise ValueError(f"Mermaid 渲染工具不存在: {renderer}")

    sources = [
        _require_direct_child(case_dir, candidate_md, "候选清单"),
        _require_direct_child(case_dir, disclosure_md, "交底书"),
        _require_direct_child(case_dir, claims_md, "权利要求"),
        _require_direct_child(case_dir, report_md, "查新报告"),
    ]
    disclosure_text = sources[1].read_text(encoding="utf-8")
    _validate_invention_disclosure(disclosure_text)
    outputs = [
        case_dir / f"01_交底书_{case_name}.docx",
        case_dir / f"02_权利要求_{case_name}.docx",
        case_dir / "03_查新报告.docx",
    ]
    if any(output.exists() for output in outputs):
        existing = next(output for output in outputs if output.exists())
        raise FileExistsError(f"拒绝覆盖既有交付文件: {existing}")

    with tempfile.TemporaryDirectory(prefix="patent-combo-finalize-", dir=case_dir) as temp_dir:
        temp_root = Path(temp_dir)
        trimmed_report = temp_root / "03_查新报告.md"
        trimmed_report.write_text(extract_report_section(sources[3].read_text(encoding="utf-8")), encoding="utf-8")
        disclosure_source = sources[1]
        disclosure_base = case_dir
        if _is_invention(disclosure_text):
            disclosure_source = temp_root / "交底书渲染稿.md"
            _render_mermaid(renderer, sources[1], disclosure_source, runner)
            disclosure_base = temp_root
        _run_converter(converter, disclosure_source, outputs[0], disclosure_base, runner)
        _run_converter(converter, sources[2], outputs[1], case_dir, runner)
        _run_converter(converter, trimmed_report, outputs[2], temp_root, runner)

    for source in sources:
        source.unlink()
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="收敛 patent-combo 的最终 DOCX 交付物")
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument("--case-name", required=True)
    parser.add_argument("--candidate-md", required=True, type=Path)
    parser.add_argument("--disclosure-md", required=True, type=Path)
    parser.add_argument("--claims-md", required=True, type=Path)
    parser.add_argument("--report-md", required=True, type=Path)
    parser.add_argument(
        "--converter",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "references/disclosure/skills/patent-disclosure/tools/md_to_docx.py",
    )
    parser.add_argument(
        "--renderer",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "references/disclosure/skills/patent-disclosure/tools/mermaid_render.py",
    )
    args = parser.parse_args(argv)
    try:
        outputs = finalize(
            case_dir=args.case_dir,
            case_name=args.case_name,
            candidate_md=args.candidate_md,
            disclosure_md=args.disclosure_md,
            claims_md=args.claims_md,
            report_md=args.report_md,
            converter=args.converter,
            renderer=args.renderer,
        )
    except (ValueError, FileExistsError, RuntimeError) as error:
        print(f"❌ {error}", file=sys.stderr)
        return 2
    for output in outputs:
        print(f"DOCX: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
