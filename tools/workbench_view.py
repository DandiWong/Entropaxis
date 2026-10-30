"""Entropaxis 团队协作工作台视图纯函数（Tech-175 M1-9）。

`stage_bar()`：阶段条七段状态（方案 11.2）；`pending_gates()`：「待我拍板」六类卡片（方案 11.3）。

硬约束：
- 纯函数：不写任何文件、不扫盘、不读当前时间以外的隐式状态；运行态（`runs`）、审计门禁数据
  （`audit_gate`）与工时同步账本（`ledger`）都由调用方注入，`now` 可注入以便固定时钟。
- 只读 `roles_manifest.assignments[]`、`task_segments` 中 `actor=role` 的段与注入的运行态；
  不读 Front Matter `status`，不读无 `actor` 的旧段及其 `stage`（P-1 根因是旧段 stage 错位）。
- 角色 → 阶段只取 `update_capsule.ROLE_STAGE`（Reviewer 按交付物前缀分方案/实施审计）。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from typing import Any

try:
    from . import check_audit_gate, update_capsule
except ImportError:  # 脚本方式运行
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import check_audit_gate  # type: ignore
    import update_capsule  # type: ignore

CST = dt.timezone(dt.timedelta(hours=8))  # 与 update_capsule.TZ_CST 同值（Q-11）
LIVE_LIFECYCLES = ("draft", "active")
PLAN_AUDIT_ROUNDS = 5  # 方案轻审默认熔断轮次，`audit_max_rounds` 可覆盖（rules/角色协作.md 熔断条）
IMPL_AUDIT_ROUNDS = 7

# 阶段条七段：(阶段条键, 标签)。编号前缀同时用于匹配 `instantiated_stages`。
SEGMENTS = (("01_调研", "调研"), ("02_方案", "方案"), ("03_设计", "设计"), ("04_Spec", "Spec"),
            ("05_审计", "审计"), ("07_验收", "验收"), ("08_汇报", "汇报"))
# `ROLE_STAGE` 值（及 Reviewer 的两个派生阶段）→ 阶段条键。
_STAGE_SLOT = {"01_调研": "01_调研", "02_方案": "02_方案", "03_方案审计": "02_方案",
               "03_设计": "03_设计", "04_实施": "04_Spec", "05_实施审计": "05_审计",
               "07_验收": "07_验收", "08_汇报": "08_汇报"}


def _now() -> dt.datetime:
    return dt.datetime.now(CST)


def _ts(value: Any) -> dt.datetime | None:
    """时间戳 → 带时区时刻；无时区视作 +08:00（同 Q-11），无法解析返回 None。"""
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=CST)


def _assignments(capsule: dict) -> list[dict]:
    manifest = capsule.get("roles_manifest") or {}
    return [a for a in (manifest.get("assignments") or []) if isinstance(a, dict)]


def _attempts(assignment: dict) -> list[dict]:
    return [a for a in (assignment.get("attempts") or []) if isinstance(a, dict)]


def _last_end(assignment: dict) -> dt.datetime | None:
    ends = [t for t in (_ts(a.get("ended_at")) for a in _attempts(assignment)) if t]
    return max(ends) if ends else None


def _starts(capsule: dict) -> list[dt.datetime]:
    return [t for a in _assignments(capsule) for at in _attempts(a)
            if (t := _ts(at.get("started_at")))]


def _is_plan_review(assignment: dict) -> bool:
    return assignment.get("role") == "Reviewer" and \
        Path(str(assignment.get("deliverable") or "")).name.startswith("03_")


def _stage_of(assignment: dict) -> str:
    return update_capsule._dispatch_stage(str(assignment.get("role")), assignment.get("deliverable"))


def _slot_of(assignment: dict) -> str | None:
    return _STAGE_SLOT.get(_stage_of(assignment))


def _alive(run: dict) -> bool:
    if "alive" in run:
        return bool(run["alive"])
    try:
        os.kill(int(run.get("pid")), 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


def _open_role_segment(capsule: dict) -> dict | None:
    """最晚开始的 open 角色段；无 `actor=role` 的旧段一律忽略。"""
    segs = [s for s in (capsule.get("task_segments") or [])
            if isinstance(s, dict) and s.get("state") == "open" and s.get("actor") == "role"]
    return max(segs, key=lambda s: _ts(s.get("startAt")) or dt.datetime.min.replace(tzinfo=CST),
               default=None)


def _is_running(seg: dict, runs: list[dict] | None) -> bool:
    did = str(seg.get("dispatch_id") or "")
    return bool(did) and any(str(r.get("dispatch_id") or "").startswith(did) and _alive(r)
                             for r in runs or [] if isinstance(r, dict))


def _seg_slot(seg: dict) -> str | None:
    stage = seg.get("stage") or update_capsule._dispatch_stage(str(seg.get("role")), None)
    return _STAGE_SLOT.get(stage)


def stage_bar(capsule: dict, *, runs: list[dict] | None = None, now=None) -> list[dict]:
    """阶段条七段：`[{stage, label, sub, state, tone, assignment}]`（方案 11.2，命中即停）。

    `state ∈ {running, residual, parked, done, todo, uninstantiated}`；`tone` 供 UI 配色：
    running/brand、residual/amber、parked 随 status（succeeded brand，其余 rose）。
    `now` 保留以对齐 `pending_gates` 签名，阶段判据本身不依赖时间。
    """
    assignments = _assignments(capsule)
    instantiated = [str(s) for s in (capsule.get("instantiated_stages") or [])]
    bar = {key: {"stage": key, "label": label, "sub": None, "state": "uninstantiated",
                 "tone": None, "assignment": None} for key, label in SEGMENTS}

    succeeded = {s for a in assignments if a.get("status") == "succeeded" and (s := _slot_of(a))}
    for a in assignments:
        if _is_plan_review(a):
            bar["02_方案"]["sub"] = "轻审"

    current_slot, current_state, tone, current_assignment = None, None, None, None
    seg = _open_role_segment(capsule)
    if seg is not None and _seg_slot(seg):
        running = _is_running(seg, runs)
        current_slot, current_state = _seg_slot(seg), "running" if running else "residual"
        tone = "brand" if running else "amber"
        did = str(seg.get("dispatch_id") or "")
        current_assignment = next(
            (a for a in assignments if any(str(at.get("dispatch_id") or "") == did
                                           for at in _attempts(a))),
            next((a for a in reversed(assignments) if a.get("role") == seg.get("role")), None))
    else:
        timed = [(end, a) for a in assignments if (end := _last_end(a))]
        if timed:
            _, latest = max(timed, key=lambda pair: pair[0])
            # Reporter 是末段：成功即整条链完成，不再有「停驻」的下一步（AC-11 ①）。
            if not (latest.get("role") == "Reporter" and latest.get("status") == "succeeded"):
                current_slot, current_state = _slot_of(latest), "parked"
                tone = "brand" if latest.get("status") == "succeeded" else "rose"
                current_assignment = latest

    for key, _ in SEGMENTS:
        entry = bar[key]
        if key == current_slot:
            entry.update(state=current_state, tone=tone, assignment=current_assignment)
        elif key in succeeded:
            entry["state"] = "done"
        elif any(stage.startswith(key[:3]) for stage in instantiated):
            entry["state"] = "todo"
    return list(bar.values())


def _card(kind: str, title: str, assignment: dict | None = None, detail: str = "") -> dict:
    return {"kind": kind, "title": title, "role": (assignment or {}).get("role"),
            "assignment_id": (assignment or {}).get("assignment_id"), "detail": detail}


def _latest(capsule: dict, pick) -> dict | None:
    timed = [(end, a) for a in _assignments(capsule) if pick(a) and (end := _last_end(a))]
    return max(timed, key=lambda pair: pair[0])[1] if timed else None


def _no_attempt_after(capsule: dict, assignment: dict, role: str | None = None) -> bool:
    end = _last_end(assignment)
    starts = [t for a in _assignments(capsule) if role is None or a.get("role") == role
              for at in _attempts(a) if (t := _ts(at.get("started_at")))]
    return end is not None and not any(t > end for t in starts)


def _blocking(audit_gate: dict | None) -> list[str] | None:
    if not audit_gate:
        return None
    phase = audit_gate.get("phase") or "plan"
    return check_audit_gate.blocking_ids(audit_gate.get("issues") or [], phase)


def pending_gates(capsule: dict, *, audit_gate: dict | None = None, ledger: list[dict] | None = None,
                  runs: list[dict] | None = None, now=None) -> list[dict]:
    """「待我拍板」六类卡片（方案 11.3）。只对 `lifecycle ∈ {draft, active}` 的胶囊出卡。

    `audit_gate`：调用方解析审计报告后注入 `{phase: plan|impl, issues: [...], max_rounds?: int}`；
    缺省时不推断阻断数，方案待确认与审计熔断两类不出卡。`ledger`：工时同步账本条目列表。
    `runs`：本机 run 文件列表，用于判定「残留待处置」。
    """
    if capsule.get("lifecycle") not in LIVE_LIFECYCLES:
        return []
    cards: list[dict] = []

    designer = _latest(capsule, lambda a: a.get("role") == "Designer")
    if designer and designer.get("status") == "succeeded" and _no_attempt_after(capsule, designer):
        cards.append(_card("prototype_review", "原型待确认", designer, "Designer 已完成，下一棒未开始"))
    builder = _latest(capsule, lambda a: a.get("role") == "Builder")
    if builder and builder.get("status") == "succeeded" and _no_attempt_after(capsule, builder):
        cards.append(_card("preview_review", "预览待确认", builder, "Builder 已完成，下一棒未开始"))

    blocking = _blocking(audit_gate)
    if blocking is not None:
        phase = audit_gate.get("phase") or "plan"
        reviews = [a for a in _assignments(capsule)
                   if a.get("role") == "Reviewer" and _is_plan_review(a) == (phase == "plan")]
        rounds = sum(1 for a in reviews for at in _attempts(a) if at.get("exit_code") == 0)
        limit = int(audit_gate.get("max_rounds") or
                    (PLAN_AUDIT_ROUNDS if phase == "plan" else IMPL_AUDIT_ROUNDS))
        if blocking and rounds >= limit:
            cards.append(_card("audit_fuse", "审计熔断转人工", None,
                               f"阻断 {len(blocking)} 条，已审 {rounds}/{limit} 轮"))
        plan_review = _latest(capsule, _is_plan_review)
        if (phase == "plan" and not blocking and plan_review and plan_review.get("status") == "succeeded"
                and _no_attempt_after(capsule, plan_review, role="Builder")):
            cards.append(_card("proposal_review", "方案待确认", plan_review, "方案审计无阻断，Builder 未开始"))

    seg_pending = any(isinstance(s, dict) and s.get("state") == "pending_approval"
                      for s in capsule.get("task_segments") or [])
    open_seg = _open_role_segment(capsule)
    residual = open_seg is not None and not _is_running(open_seg, runs)
    ledger_bad = any(isinstance(e, dict) and e.get("status") in ("unknown", "needs_manual_fix")
                     for e in ledger or [])
    if seg_pending or residual or ledger_bad:
        cards.append(_card("worktime_review", "工时待核决", None,
                           "待核决段 / 残留角色段 / 工时同步账本异常"))

    for a in _assignments(capsule):
        if a.get("status") not in ("failed", "blocked"):
            continue
        end = _last_end(a)
        reassigned = any(b is not a and b.get("role") == a.get("role") and
                         any((t := _ts(at.get("started_at"))) and end and t > end for at in _attempts(b))
                         for b in _assignments(capsule))
        if not reassigned:
            cards.append(_card("dispatch_failed", "调度失败", a, f"状态 {a.get('status')}"))
    return cards



def today_cst(now: dt.datetime | None = None) -> str:
    return _ts(now or _now()).astimezone(CST).date().isoformat()


def week_since(now: dt.datetime | None = None) -> str:
    """本周起点（ISO 周一，固定 +08:00），作为 `aggregate_worklog.py --since` 的值（Q-11）。"""
    local = _ts(now or _now()).astimezone(CST)
    return (local.date() - dt.timedelta(days=local.weekday())).isoformat()


def week_bucket(ts: Any, now: dt.datetime | None = None) -> str:
    """时间戳落在 `this` / `last` / `other` 周：先换算到 +08:00 再分桶（半开区间 [周一 00:00, 下周一 00:00)）。"""
    moment = _ts(ts)
    if moment is None:
        return "other"
    monday = dt.datetime.fromisoformat(week_since(now)).replace(tzinfo=CST)
    if monday <= moment < monday + dt.timedelta(days=7):
        return "this"
    if monday - dt.timedelta(days=7) <= moment < monday:
        return "last"
    return "other"
