#!/usr/bin/env python3
"""审计报告 ``audit-state`` 围栏的字段级读写。

真源定义: rules/角色协作.md「问题终态三分」· rules/治理指令.md「审计」§5
机器契约: schemas/audit_report.schema.json 的 sidecar_format

存在的理由（output token）：
    schema_version 3 起机器状态的唯一真源是正文内唯一 ```audit-state``` 围栏。此前没有
    字段级写入工具，模型每轮改一个 status 都要重打整段 JSON——输出随问题数 O(N) 且逐轮
    重复，还容易在重打时压坏同伴条目。这正是《工具设计》3.5「结构化状态外置」点名要消除的
    形态：机器要判定的状态不该靠模型逐字重述。

设计不变量:
  1. **不复制门禁策略**：本工具不自行判断"这条 Critical 能不能关"。写入前把候选全文交给
     check_audit_gate.check_candidate_commit 校验，它放行才落盘。Critical 关闭与
     waived_by_user 的判据只有那一份实现，此处一行都不重写——两份策略必然漂移，而漂移的
     那一份就是绕过独立复核的后门。
  2. **原子写入**：tempfile 同目录组装 + os.replace，中途失败不留半截文件。
  3. **围栏解析复用**：正则与重复键拒绝都直接用 check_audit_gate 的实现，不另起一套解析。
  4. **改动即失去盖章**：调度回执绑定的是含围栏的正文，改任何字段都会与回执失配。
     写入前不查回执（与 dispatch_role 签发前的判据同语义），写入时一并删去 receipt_id：
     报告若自称外置承载，完整核验即报"未携带 receipt_id"而阻断，须由下一轮真实
     Reviewer 调度重新盖章才恢复有效——会话改动（含越权关闭 Critical）只会以
     "报告失效"的形式暴露，不会挂着旧回执冒充外置结论。
"""

from __future__ import annotations

import argparse
import json
import sys
import re
import tempfile
from pathlib import Path

try:
    from . import check_audit_gate as cag
    from . import update_capsule
except ImportError:  # 直接 python3 tools/update_audit_state.py 调用时
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import check_audit_gate as cag
    import update_capsule


class AuditStateError(Exception):
    """可恢复的业务异常，携带行动导向修复指引。"""


def load_state(text: str) -> dict:
    """取出围栏内 JSON。围栏数量与重复键的判据复用 check_audit_gate。"""
    raw, issues = cag._extract_audit_state(text)
    if raw is None:
        raise AuditStateError(
            "❌ 未能定位唯一的 ```audit-state``` 围栏：" + "；".join(issues) + "\n"
            "👉 修复建议: schema_version 3 的报告必须恰好含 1 个 audit-state 围栏；"
            "老报告（schema_version < 3）不适用本工具，请按其自身契约处理。"
        )
    try:
        return json.loads(raw, object_pairs_hook=cag._reject_duplicate_keys)
    except ValueError as err:
        raise AuditStateError(
            f"❌ audit-state 围栏内不是合法 JSON: {err}\n"
            "👉 修复建议: 先人工修复围栏内 JSON 语法，再重试本工具。"
        ) from err


def dump_state(text: str, state: dict) -> str:
    """把改动后的 state 写回围栏，正文其余部分逐字不动。"""
    body = json.dumps(state, ensure_ascii=False, indent=2)

    def _replace(match):
        fence = match.group("fence")
        return f"{fence}audit-state\n{body}\n{fence}"

    new_text, count = cag._AUDIT_STATE_FENCE.subn(_replace, text, count=1)
    if count != 1:
        raise AuditStateError(
            "❌ 回写 audit-state 围栏失败（未匹配到围栏）。\n"
            "👉 修复建议: 确认报告未在读取后被并发修改，然后重试。"
        )
    return new_text


# v4 问题准入字段：有 {field}_enum 的走枚举，其余只要求非空；是否必填由门禁判定，本工具不复制判据。
V4_FIELDS = ("gate", "basis", "evidence", "repro")
_RECEIPT_LINE = re.compile(r"^receipt_id:.*\n", re.MULTILINE)


def _check_value(field: str, value: str) -> None:
    schema = cag.vs.load_schema("audit_report")
    if f"{field}_enum" in schema:
        _reject_unknown(field, value, schema[f"{field}_enum"])
    elif not value.strip():
        raise AuditStateError(f"❌ {field} 不能为空。\n👉 修复建议: 写明取值后重试。")


def add_issue(state: dict, issue_id: str, level: str, status: str = "open", **extra: str | None) -> dict:
    issues = state.setdefault("issues", [])
    if any(i.get("id") == issue_id for i in issues):
        raise AuditStateError(
            f"❌ 问题 ID {issue_id!r} 已存在，不得重复登记。\n"
            "👉 修复建议: 换一个事件内稳定且未使用过的 ID，或改用 --set 更新既有条目。"
        )
    _reject_unknown("level", level, cag.vs.load_schema("audit_report")["level_enum"])
    _reject_unknown("status", status, cag.vs.load_schema("audit_report")["status_enum"])
    item = {"id": issue_id, "level": level, "status": status}
    for field in V4_FIELDS:
        if extra.get(field) is not None:
            _check_value(field, extra[field])
            item[field] = extra[field]
    issues.append(item)
    state.setdefault("critical_acks", [])
    return state


def set_field(state: dict, issue_id: str, field: str, value: str) -> dict:
    if field not in ("level", "status") + V4_FIELDS:
        raise AuditStateError(
            f"❌ 不支持的字段 {field!r}（仅 level / status / {' / '.join(V4_FIELDS)} 可改）。\n"
            "👉 修复建议: critical_acks 属用户风险接受记录，须人工确认后单独维护，不由本工具代写。"
        )
    _check_value(field, value)
    for issue in state.get("issues", []):
        if issue.get("id") == issue_id:
            issue[field] = value
            return state
    raise AuditStateError(
        f"❌ 未找到问题 ID {issue_id!r}。\n"
        "👉 修复建议: 先用 --list 查看现有条目，确认 ID 拼写。"
    )


def _reject_unknown(field: str, value: str, allowed: list[str]) -> None:
    if value not in allowed:
        raise AuditStateError(
            f"❌ {field}={value!r} 不在允许取值 {allowed} 内。\n"
            f"👉 修复建议: 取值域真源是 schemas/audit_report.schema.json 的 {field}_enum。"
        )


def _strip_receipt(text: str) -> tuple[str, bool]:
    """删去 Front Matter 的 receipt_id 行（与 dispatch_role.stamp_carrier 的 "" 删除语义一致）。"""
    fm = cag.FRONT_MATTER_PATTERN.search(text)
    if not fm:
        return text, False
    block, n = _RECEIPT_LINE.subn("", fm.group(1) + "\n")
    return text[:fm.start(1)] + block.rstrip("\n") + text[fm.end(1):], n > 0


def commit(path: Path, new_text: str) -> bool:
    """经 check_audit_gate 放行后才原子落盘——门禁策略只有那一份实现。

    返回是否删去了回执（设计不变量 4）。"""
    violations = cag.check_candidate_commit(new_text, path, receipt_check=False)
    if violations:
        raise AuditStateError(
            "❌ 候选状态未通过审计门禁，未写入:\n  - " + "\n  - ".join(violations) + "\n"
            "👉 修复建议: 按上列逐条补齐——v4 的 open 问题须带 gate/basis/evidence（--gate/--basis/--evidence）；"
            "Critical 的 closed/withdrawn 须外置 Reviewer 复核，waived_by_user 须有指纹匹配的 critical_ack；"
            "本工具不提供绕过路径。"
        )
    new_text, unsigned = _strip_receipt(new_text)
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        tmp_path = Path(tmp) / path.name
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(path)
    # 问题关闭可能让方案过审：顺带推进所在胶囊的 lifecycle（不在胶囊内则无动作）
    update_capsule.sync_for(path)
    return unsigned


def blocking_summary(issues: list, phase: str) -> str:
    """供 Manager 判断放行与熔断的一行汇总；阻断判据只调用 check_audit_gate.blocking_ids。"""
    blocking = cag.blocking_ids(issues, phase)
    open_items = [i for i in issues if i.get("status") == "open"]
    count = lambda g: sum(1 for i in open_items if i.get("gate") == g)  # noqa: E731
    later = count("impl") if phase == "plan" else 0
    return (f"本阶段（{phase}）阻断 {len(blocking)}（{', '.join(blocking) or '无'}）｜待实施 {later}"
            f"｜上线前提 {count('release')}｜另立事项 {count('none')}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="审计报告 audit-state 围栏的字段级读写（Critical 关闭判据仍归 check_audit_gate.py）",
    )
    parser.add_argument("report", help="审计报告路径（05_审计报告.md / Audit_<ID>_*.md）")
    parser.add_argument("--list", action="store_true", help="只读列出现有问题条目")
    parser.add_argument("--add-issue", metavar="ID:LEVEL", help="登记新问题，如 C-2:Critical")
    parser.add_argument("--status", default="open", help="配合 --add-issue 的初始状态，默认 open")
    for field, hint in (("gate", "plan|impl|release|none"), ("basis", "引用 G-/NG-/AC- 或项目硬约束"),
                        ("evidence", "measured|traced|inferred"), ("repro", "失败测试或复现命令")):
        parser.add_argument(f"--{field}", help=f"配合 --add-issue（v4）：{hint}")
    parser.add_argument("--set", nargs=2, metavar=("ID", "FIELD=VALUE"),
                        help="更新既有问题字段，如 --set C-2 status=closed")
    parser.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    args = parser.parse_args()

    path = Path(args.report).expanduser()
    if not path.is_file():
        print(f"❌ 审计报告不存在: {path}\n👉 修复建议: 用 find_capsule.py --latest-artifact audit 定位。",
              file=sys.stderr)
        return 2

    try:
        text = path.read_text(encoding="utf-8")
        state = load_state(text)

        if args.list or not (args.add_issue or args.set):
            issues = state.get("issues", [])
            if args.json:
                print(json.dumps(state, ensure_ascii=False, indent=2))
            else:
                for i in issues:
                    print(f"{i['id']}\t{i['level']}\t{i['status']}\t{i.get('gate', '-')}\t{i.get('evidence', '-')}")
                print(f"# {len(issues)} 条，其中 open {sum(1 for i in issues if i['status'] == 'open')} 条")
                phase = cag.parse_front_matter(text).get("audit_phase")
                if phase in cag.GATE_ORDER:
                    print("# " + blocking_summary(issues, phase))
            return 0

        if args.add_issue:
            issue_id, _, level = args.add_issue.partition(":")
            if not issue_id or not level:
                raise AuditStateError(
                    "❌ --add-issue 格式应为 ID:LEVEL（如 C-2:Critical）。\n"
                    "👉 修复建议: 补上冒号与级别后重试。"
                )
            state = add_issue(state, issue_id.strip(), level.strip(), args.status.strip(),
                              **{f: getattr(args, f) for f in V4_FIELDS})
        if args.set:
            issue_id, assignment = args.set
            field, _, value = assignment.partition("=")
            if not field or not value:
                raise AuditStateError(
                    "❌ --set 第二个参数应为 FIELD=VALUE（如 status=closed）。\n"
                    "👉 修复建议: 补上等号与取值后重试。"
                )
            state = set_field(state, issue_id.strip(), field.strip(), value.strip())

        unsigned = commit(path, dump_state(text, state))
        if unsigned and not args.json:
            print("⚠️ 状态已推进，已删去 receipt_id：报告回到待复核态，下一轮 Reviewer 复核后重新盖章才恢复有效。",
                  file=sys.stderr)
        counts = {}
        for i in state.get("issues", []):
            counts[i["status"]] = counts.get(i["status"], 0) + 1
        summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(json.dumps({"ok": True, "report": str(path), "counts": counts, "receipt_cleared": unsigned},
                         ensure_ascii=False)
              if args.json else f"✅ 已更新 {path.name}: {summary}")
        return 0
    except AuditStateError as err:
        print(str(err), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
