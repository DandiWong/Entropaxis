#!/usr/bin/env python3
"""审计报告 Critical 问题关闭硬门禁 (Audit Critical-Closure Gate).

把《角色协作.md》「问题终态三分」——"Critical 只能由外置 Reviewer 复核关闭；
用户显式风险接受走 waived_by_user，且不得记为 closed"——从纯文字约束转成
可执行的机械阻断。

两种用法：
  只读复核（兼容旧用法）：
    python3 .system/tools/check_audit_gate.py <审计报告路径>
  原子校验并提交（拟写入的关闭后状态一次性校验+落盘，closed 与 waived_by_user 均须过此关）：
    python3 .system/tools/check_audit_gate.py --candidate <候选报告路径> --commit <目标路径>

字段契约见 .system/schemas/audit_report.schema.json。schema_version 缺失
按旧 independence 契约只读解析；**只要 Front Matter 出现 schema_version 键**（无论是否
能解析为合法整数）即视为声明使用新契约，全量校验该契约——不会因为版本号写错就静默
退回旧契约放行（第 4 轮外置复核实测抓到的 fail-open）。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tempfile
from pathlib import Path

try:
    from tools import validate_schema as vs
except ImportError:  # 以脚本方式直接运行时 tools/ 自身在 sys.path 上
    import validate_schema as vs

FRONT_MATTER_PATTERN = re.compile(r"^---\n(.*?)\n---", re.DOTALL)

# 单条问题块：从"级别 : X"起，到下一个标题/分隔线/文末为止。冒号前允许空格
# （第 5 轮外置复核实测抓到：`级别 : Critical` 因冒号前紧邻不容空格而整块不被
# 识别，导致状态判定被跳过——这是本文件里第二处"正则比真实解析器更脆弱"的
# 实例，第一处是 schema_version 判别，已改为直接复用 parse_front_matter）。
# group(1)=块全文，group(2)=级别。
ISSUE_BLOCK_PATTERN = re.compile(
    r"(级别\s*[:：]\s*(Critical|Major|Minor).*?)(?=\n#{1,6}\s|\n---|\Z)", re.DOTALL
)
ISSUE_ID_PATTERN = re.compile(r"ID\s*[:：]\s*(\S+)")
STATUS_VALUE_PATTERN = re.compile(r"状态\s*[:：]\s*(\S+)")
LEGACY_CLOSED_PATTERN = re.compile(r"状态\s*[:：]\s*(已关闭|closed)", re.IGNORECASE)
V2_STATUS_ENUM = ("open", "closed", "waived_by_user")

# critical_ack 确认块：### critical_ack <问题ID> 后跟 target_sha256/确认事件/适用范围 三行。
ACK_BLOCK_PATTERN = re.compile(
    r"###\s*critical_ack\s+(\S+)\s*\n(.*?)(?=\n#{1,3}\s|\Z)", re.DOTALL
)


def _front_matter_text(text: str) -> str | None:
    match = FRONT_MATTER_PATTERN.search(text)
    return match.group(1) if match else None


def parse_front_matter(text: str) -> dict:
    """复用 validate_schema 的扁平 Front Matter 解析；无档头返回空字典。"""
    return vs.parse_front_matter(text) or {}


def is_v2(text: str) -> bool:
    """Front Matter 出现 schema_version 键即为 v2 意图，不要求其值合法——
    值是否合法由完整 schema 校验负责报错，不在此处静默降级。

    直接复用 parse_front_matter()（等价 validate_schema 的解析语义：按首个冒号切分、
    两侧 strip），不再用独立正则检测键是否存在——此前 `schema_version : 2`（冒号前带
    空格）能被 parse_front_matter 正确解析出键，却被本函数的严格正则判定为"键不存在"，
    致使整份 v2 报告被错误地当成旧契约放行（第 5 轮外置复核实测抓到的 fail-open）。
    """
    return "schema_version" in parse_front_matter(text)


def extract_independence(text: str) -> str | None:
    """读取 Front Matter 中的 independence 声明（旧契约）；未声明返回 None。"""
    fm = parse_front_matter(text)
    val = fm.get("independence")
    return str(val).strip() if val is not None else None


def _iter_issue_blocks(text: str):
    """逐条产出问题块 (级别, 块全文)。"""
    for m in ISSUE_BLOCK_PATTERN.finditer(text):
        yield m.group(2), m.group(1)


def find_closed_critical_blocks(text: str) -> list[str]:
    """返回正文中已标注关闭（旧契约"已关闭"/"closed"）的 Critical 问题块。"""
    return [b for level, b in _iter_issue_blocks(text) if level == "Critical" and LEGACY_CLOSED_PATTERN.search(b)]


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
    """无 schema_version 键：旧 independence 契约（会话内降级不可关闭 Critical）。"""
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
    """Front Matter 含 schema_version 键：完整 v2 契约校验，不允许部分校验后放行。"""
    issues: list[str] = []
    fm = parse_front_matter(text)

    # 1) 完整 Front Matter 结构校验（必填字段、类型、枚举、条件必填）——
    #    此前只做了零星正则抽取，缺字段/非法枚举/畸形版本号全部悄悄放行。
    schema = vs.load_schema("audit_report")["properties"]["front_matter"]
    issues += [f"Front Matter {e}" for e in vs.validate(fm, schema)]

    # 2) v2 报告禁止再写已退役的 independence 字段（generic validator 不表达"字段互斥"）。
    if "independence" in fm:
        issues.append("schema_version>=2 的报告不得再声明 independence（已退役字段，只供历史报告只读兼容）。")

    reviewer_mode = fm.get("reviewer_mode")
    target_sha256 = fm.get("target_sha256")
    acks = find_ack_blocks(text)

    for level, block in _iter_issue_blocks(text):
        id_match = ISSUE_ID_PATTERN.search(block)
        status_match = STATUS_VALUE_PATTERN.search(block)
        # 3) v2 issue_format 声明 id_field/status_field 为必填；此前缺失时直接跳过
        #    该块全部校验（第 5 轮实测：缺 ID 或缺状态行的块可静默通过）。
        if id_match is None:
            issues.append(f"[{level} 问题块] 缺少可识别的 `ID:` 标注，v2 契约要求每条问题有稳定 ID。")
            continue
        issue_id = id_match.group(1)
        if status_match is None:
            issues.append(f"[{issue_id}] 缺少可识别的 `状态:` 标注。")
            continue
        status = status_match.group(1)

        # 4) 状态枚举强制校验：v2 只认 open/closed/waived_by_user，其余（含 challenged）一律违规。
        if status not in V2_STATUS_ENUM:
            issues.append(f"[{issue_id}] 状态={status!r} 不在 v2 枚举 {V2_STATUS_ENUM} 内。")
            continue

        if level == "Critical" and status == "closed":
            if reviewer_mode != "external":
                issues.append(
                    f"[{issue_id}] 状态=closed 但 reviewer_mode={reviewer_mode!r}；"
                    "会话内承载不可将 Critical 置为 closed，必须由外置 reviewer 复核。"
                )
        if status == "waived_by_user":
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
    return issues


def check_report(text: str) -> list[str]:
    """对审计报告全文做门禁核验，返回违规说明列表（空列表即通过）。"""
    return check_report_v2(text) if is_v2(text) else check_report_legacy(text)


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
    """--candidate/--commit 原子接口的额外校验：target_sha256 与目标受审文件实测哈希一致。

    先跑 check_report()（含完整 Front Matter 校验），字段本身缺失/非法已在那一步拦截；
    这里只做 check_report() 管不到的"文件系统事实校验"——target_path 是否真实存在、
    其内容哈希是否与声明一致。
    """
    issues = check_report(candidate_text)
    if not is_v2(candidate_text):
        return issues
    fm = parse_front_matter(candidate_text)
    target_path, target_sha256 = fm.get("target_path"), fm.get("target_sha256")
    if not target_path or not target_sha256:
        return issues  # 缺字段已由 check_report() 的 schema 校验报出，这里不重复
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
