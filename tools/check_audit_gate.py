#!/usr/bin/env python3
"""审计报告 Critical 问题关闭硬门禁 (Audit Critical-Closure Gate).

把《角色协作.md》「问题终态三分」——"Critical 只能由外置 Reviewer 复核关闭；
用户显式风险接受走 waived_by_user，且不得记为 closed"——从纯文字约束转成
可执行的机械阻断。

两种用法：
  只读复核（兼容旧用法）：
    python3 .system/tools/check_audit_gate.py <审计报告路径>
  原子校验并提交（拟写入的关闭后状态一次性校验+落盘）：
    python3 .system/tools/check_audit_gate.py --candidate <候选报告路径> --commit <目标路径>

字段契约见 .system/schemas/audit_report.schema.json。schema_version 缺失或 <2
按旧 independence 契约只读解析；>=2 按新契约（reviewer_mode/critical_ack）解析。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tempfile
from pathlib import Path

FRONT_MATTER_PATTERN = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
INDEPENDENCE_PATTERN = re.compile(r"^independence:\s*(.+)$", re.MULTILINE)
SCHEMA_VERSION_PATTERN = re.compile(r"^schema_version:\s*(\d+)$", re.MULTILINE)
REVIEWER_MODE_PATTERN = re.compile(r"^reviewer_mode:\s*(\S+)$", re.MULTILINE)
TARGET_PATH_PATTERN = re.compile(r"^target_path:\s*(\S+)$", re.MULTILINE)
TARGET_SHA256_PATTERN = re.compile(r"^target_sha256:\s*([0-9a-f]{64})$", re.MULTILINE)

# 单条问题块：从"级别: X"起，到下一个标题/分隔线/文末为止。group(1)=块全文，group(2)=级别。
ISSUE_BLOCK_PATTERN = re.compile(
    r"(级别[:：]\s*(Critical|Major|Minor).*?)(?=\n#{1,6}\s|\n---|\Z)", re.DOTALL
)
ISSUE_ID_PATTERN = re.compile(r"ID[:：]\s*(\S+)")
CLOSED_PATTERN = re.compile(r"状态[:：]\s*(已关闭|closed)", re.IGNORECASE)
WAIVED_PATTERN = re.compile(r"状态[:：]\s*waived_by_user", re.IGNORECASE)

# critical_ack 确认块：### critical_ack <问题ID> 后跟 target_sha256/确认事件/适用范围 三行。
ACK_BLOCK_PATTERN = re.compile(
    r"###\s*critical_ack\s+(\S+)\s*\n(.*?)(?=\n#{1,3}\s|\Z)", re.DOTALL
)


def _front_matter_text(text: str) -> str | None:
    match = FRONT_MATTER_PATTERN.search(text)
    return match.group(1) if match else None


def extract_independence(text: str) -> str | None:
    """读取 Front Matter 中的 independence 声明（旧契约）；未声明返回 None。"""
    fm = _front_matter_text(text)
    if fm is None:
        return None
    m2 = INDEPENDENCE_PATTERN.search(fm)
    return m2.group(1).strip() if m2 else None


def extract_schema_version(text: str) -> int:
    """缺失或非数字一律视为 0（走旧契约分支）。"""
    fm = _front_matter_text(text)
    if fm is None:
        return 0
    m = SCHEMA_VERSION_PATTERN.search(fm)
    return int(m.group(1)) if m else 0


def extract_reviewer_mode(text: str) -> str | None:
    fm = _front_matter_text(text)
    if fm is None:
        return None
    m = REVIEWER_MODE_PATTERN.search(fm)
    return m.group(1).strip() if m else None


def extract_target(text: str) -> tuple[str | None, str | None]:
    fm = _front_matter_text(text)
    if fm is None:
        return None, None
    p = TARGET_PATH_PATTERN.search(fm)
    h = TARGET_SHA256_PATTERN.search(fm)
    return (p.group(1).strip() if p else None), (h.group(1).strip() if h else None)


def _iter_issue_blocks(text: str):
    """逐条产出问题块 (级别, 块全文)。"""
    for m in ISSUE_BLOCK_PATTERN.finditer(text):
        yield m.group(2), m.group(1)


def find_closed_critical_blocks(text: str) -> list[str]:
    """返回正文中已标注关闭（旧契约"已关闭"/"closed"）的 Critical 问题块。"""
    return [b for level, b in _iter_issue_blocks(text) if level == "Critical" and CLOSED_PATTERN.search(b)]


def find_ack_blocks(text: str) -> dict[str, dict[str, str]]:
    """解析正文 critical_ack 确认块，返回 {问题ID: {target_sha256, 确认事件, 适用范围}}。"""
    acks: dict[str, dict[str, str]] = {}
    for issue_id, body in ACK_BLOCK_PATTERN.findall(text):
        fields: dict[str, str] = {}
        for key, pattern in (
            ("target_sha256", r"target_sha256[:：]\s*([0-9a-f]{64})"),
            ("确认事件", r"确认事件[:：]\s*(\S.*)"),
            ("适用范围", r"适用范围[:：]\s*(\S.*)"),
        ):
            m = re.search(pattern, body)
            if m:
                fields[key] = m.group(1).strip()
        acks[issue_id] = fields
    return acks


def check_report_legacy(text: str) -> list[str]:
    """schema_version < 2：旧 independence 契约（会话内降级不可关闭 Critical）。"""
    independence = extract_independence(text)
    closed = find_closed_critical_blocks(text)
    if not closed:
        return []
    if not independence:
        return [
            f"检测到 {len(closed)} 处 Critical 问题标记为已关闭，但 Front Matter 未声明 "
            "independence。审计独立性无法核验的报告不得关闭 Critical。"
        ]
    if independence.startswith("session-internal"):
        return [
            f"independence={independence!r} 属于会话内降级，但检测到 {len(closed)} 处 Critical "
            "问题标记为已关闭；会话内自评不可关闭 Critical，必须由外置 reviewer 复核或转人工仲裁。"
        ]
    return []


def check_report_v2(text: str) -> list[str]:
    """schema_version >= 2：reviewer_mode + critical_ack 契约。"""
    issues: list[str] = []
    reviewer_mode = extract_reviewer_mode(text)
    target_path, target_sha256 = extract_target(text)
    acks = find_ack_blocks(text)

    for level, block in _iter_issue_blocks(text):
        id_match = ISSUE_ID_PATTERN.search(block)
        issue_id = id_match.group(1) if id_match else "<未知ID>"

        if level == "Critical" and CLOSED_PATTERN.search(block):
            if reviewer_mode != "external":
                issues.append(
                    f"[{issue_id}] 状态=closed 但 reviewer_mode={reviewer_mode!r}；"
                    "会话内承载不可将 Critical 置为 closed，必须由外置 reviewer 复核。"
                )
        if WAIVED_PATTERN.search(block):
            ack = acks.get(issue_id)
            if not ack:
                issues.append(f"[{issue_id}] 状态=waived_by_user 但未找到对应 critical_ack 确认块。")
                continue
            missing = [k for k in ("target_sha256", "确认事件", "适用范围") if not ack.get(k)]
            if missing:
                issues.append(f"[{issue_id}] critical_ack 缺字段：{', '.join(missing)}。")
            elif ack.get("target_sha256") != target_sha256:
                issues.append(
                    f"[{issue_id}] critical_ack.target_sha256={ack.get('target_sha256')!r} "
                    f"与报告 target_sha256={target_sha256!r} 不一致，复核对象已变化。"
                )
    if target_path and target_sha256 is None:
        issues.append("Front Matter 声明了 target_path 但缺少 target_sha256。")
    return issues


def check_report(text: str) -> list[str]:
    """对审计报告全文做门禁核验，返回违规说明列表（空列表即通过）。按 schema_version 分支。"""
    version = extract_schema_version(text)
    return check_report_v2(text) if version >= 2 else check_report_legacy(text)


def _find_workspace_root(start: Path) -> Path | None:
    """从 start 向上找到以 `.system` 为子目录的工作区根；找不到返回 None。"""
    current = start.resolve()
    for _ in range(8):
        if (current / ".system").is_dir():
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


def check_candidate_commit(candidate_text: str, commit_path: Path) -> list[str]:
    """--candidate/--commit 原子接口的额外校验：target_sha256 与目标受审文件实测哈希一致。"""
    issues = check_report(candidate_text)
    target_path, target_sha256 = extract_target(candidate_text)
    version = extract_schema_version(candidate_text)
    if version >= 2 and target_path:
        # 受审对象多数与报告同容器目录（本轮实践即如此）；找不到时退化为按工作区根解析。
        candidates = [commit_path.parent / target_path]
        workspace_root = _find_workspace_root(commit_path.parent)
        if workspace_root:
            candidates.append(workspace_root / target_path)
        actual_file = next((p for p in candidates if p.is_file()), None)
        if actual_file is None:
            issues.append(f"target_path 指向的受审文件不存在：{target_path}")
        else:
            actual_hash = hashlib.sha256(actual_file.read_bytes()).hexdigest()
            if actual_hash != target_sha256:
                issues.append(
                    f"target_sha256={target_sha256!r} 与受审文件实测哈希 {actual_hash!r} 不一致；"
                    "受审对象已变化，不得据此关闭问题。"
                )
    return issues


def atomic_write(path: Path, content: str) -> None:
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        tmp_path = Path(tmp) / path.name
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="审计报告 Critical 关闭门禁")
    parser.add_argument("path", nargs="?", help="只读模式：待校验的审计报告路径")
    parser.add_argument("--candidate", help="原子模式：候选报告路径（拟写入的关闭后状态）")
    parser.add_argument("--commit", help="原子模式：校验通过后写入的目标路径")
    args = parser.parse_args()

    if args.candidate or args.commit:
        if not (args.candidate and args.commit):
            print("用法: --candidate 与 --commit 必须同时提供", file=sys.stderr)
            return 2
        candidate_path = Path(args.candidate)
        commit_path = Path(args.commit)
        if not candidate_path.is_file():
            print(f"[候选报告不存在] {candidate_path}", file=sys.stderr)
            return 2
        candidate_text = candidate_path.read_text(encoding="utf-8")
        violations = check_candidate_commit(candidate_text, commit_path)
        if violations:
            for v in violations:
                print(f"[Critical违规关闭] {candidate_path}: {v}", file=sys.stderr)
            print(f"❌ 校验未通过，未写入 {commit_path}。", file=sys.stderr)
            return 1
        atomic_write(commit_path, candidate_text)
        print(f"✅ 校验通过，已原子写入 {commit_path}。")
        return 0

    if not args.path:
        print("用法: python3 check_audit_gate.py <审计报告路径>", file=sys.stderr)
        print("  或: python3 check_audit_gate.py --candidate <候选> --commit <目标>", file=sys.stderr)
        return 2
    path = Path(args.path)
    if not path.is_file():
        print(f"[审计报告不存在] {path}", file=sys.stderr)
        return 2
    issues = check_report(path.read_text(encoding="utf-8"))
    if issues:
        for issue in issues:
            print(f"[Critical违规关闭] {path}: {issue}", file=sys.stderr)
        return 1
    print(f"✅ {path} 未发现会话内降级下的 Critical 违规关闭。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
