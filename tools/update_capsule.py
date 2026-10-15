#!/usr/bin/env python3
"""
胶囊生命周期推进：按胶囊内已有证据把 capsule.yaml 的 lifecycle 及交付日期往前推。

状态只进不退，终态（archived / archived-unmeasured）不再改动：
  draft    → active               05_审计报告.md 无 open 的 Critical/Major，或已有 04_Spec_*
  active   → delivered            仅显式 --delivered [YYYY-MM-DD]（"合入主干且全量测试通过"无法从胶囊文件推断）
                                  同时写 delivered_at，派生 review_due_at = +30 天、closure_deadline = +60 天
  delivered→ archived             07_验收报告.md「数据回收日期」已回填
  delivered→ archived-unmeasured  07_验收报告.md「未度量兜底说明」已回填（且数据回收日期未回填）

只改 capsule.yaml 中对应的顶层键值，保留注释与其余内容；写入走临时文件后原子替换。
调用方：update_audit_state.py 写报告后自动同步；《软件工程》「收尾分支」合入后显式 --delivered；
lint_workspace.py 用 derive() 检查状态落后。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ORDER = ("draft", "active", "delivered", "archived", "archived-unmeasured")
TERMINAL = ("archived", "archived-unmeasured")
REVIEW_DAYS, CLOSURE_DAYS = 30, 60
BLOCKING = ("Critical", "Major")
TZ_CST = dt.timezone(dt.timedelta(hours=8))

# 工时登记契约（schemas/capsule.schema.json 的 worklogs 条目）。
WORKLOG_STAGE_RE = re.compile(r"^(\d{2}_\S+|misc)$")
WORKLOG_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WORKLOG_ITEM_RE = re.compile(r"^\s*-\s")
WORKLOG_DEFAULT_INDENT = "  "


class ToolError(Exception):
    """可恢复业务异常，包含行动导向修复指引。"""


def _yaml_str(value: str) -> str:
    """自由文本 → JSON 双引号标量（JSON 字符串语法是 YAML 双引号标量的严格子集）。"""
    return json.dumps(value, ensure_ascii=False)


def _fmt_hours(value: float) -> str:
    """1.5 → '1.5'，2.0 → '2'：Trailer 与提示语都用同一格式，不出现 2.0h 这种噪声。"""
    return f"{value:g}"


def normalize_to_cst(iso_str: str) -> dt.datetime:
    """归一化到 +08:00 时区。"""
    try:
        d = dt.datetime.fromisoformat(iso_str)
    except Exception as exc:
        raise ToolError(f"❌ 时间格式不合法: {iso_str}（须为 ISO 8601，如 2026-10-15T10:00:00+08:00）") from exc
    if d.tzinfo is None:
        return d.replace(tzinfo=TZ_CST)
    return d.astimezone(TZ_CST)


def q_minutes(m: float) -> float:
    """取整函数 q(m) = max(0.25, floor(m / 15 + 0.5) / 4)（仅当 m > 0）。"""
    if m <= 0:
        raise ToolError(f"❌ 时长必须大于 0 分钟 (m={m})")
    steps = math.floor(m / 15.0 + 0.5)
    return max(0.25, steps / 4.0)


def _validate_entry(entry: dict) -> list[str]:
    """用 capsule.schema.json 的 worklogs 条目契约校验单条记录；空列表表示通过。"""
    try:
        try:
            from . import validate_schema
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import validate_schema
        schema = validate_schema.load_schema("capsule")
    except (ImportError, OSError, ValueError) as exc:
        raise ToolError(
            f"❌ 无法读取工时契约 schemas/capsule.schema.json: {exc}\n"
            "👉 修复建议: 确认 .entropaxis/schemas/capsule.schema.json 存在且为合法 JSON。"
        ) from exc
    return validate_schema.validate(entry, schema["properties"]["worklogs"]["items"])


def _validate_segment(segment: dict) -> list[str]:
    """用 capsule.schema.json 的 task_segments 条目契约校验单条记录；空列表表示通过。"""
    try:
        try:
            from . import validate_schema
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import validate_schema
        schema = validate_schema.load_schema("capsule")
    except (ImportError, OSError, ValueError) as exc:
        raise ToolError(
            f"❌ 无法读取契约 schemas/capsule.schema.json: {exc}\n"
            "👉 修复建议: 确认 .entropaxis/schemas/capsule.schema.json 存在且为合法 JSON。"
        ) from exc
    return validate_schema.validate(segment, schema["properties"]["task_segments"]["items"])


def _default_operator() -> str:
    """登记人缺省取 git config user.name，再退环境变量；都取不到即报错，不写空值。"""
    proc = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
    name = proc.stdout.strip() if proc.returncode == 0 else ""
    return name or os.environ.get("USER") or os.environ.get("USERNAME") or ""


def _default_stage(capsule: Path) -> tuple[str, str]:
    """--stage 缺省时取胶囊内 mtime 最新的 NN_*.md 阶段名。"""
    stages = [p for p in capsule.glob("*.md") if re.match(r"^\d{2}_", p.name)]
    if not stages:
        return "misc", "default"
    newest = max(stages, key=lambda p: p.stat().st_mtime)
    return newest.name[:-3], "mtime"


def _capsule_id(capsule: Path, text: str) -> str:
    cid = _read_key(text, "id")
    if cid:
        return cid
    m = re.search(r"^\d{8}_(.*)$", capsule.name)
    return m.group(1) if m else capsule.name


def _load_capsule_data(capsule: Path) -> tuple[dict[str, Any], str]:
    """使用 PyYAML（有则使用）或极简解析器读取 capsule 数据及全文。"""
    manifest = capsule / "capsule.yaml"
    if not manifest.is_file():
        raise ToolError(f"❌ 未找到胶囊清单: {manifest}\n👉 修复建议: 传入含 capsule.yaml 的胶囊目录。")
    text = manifest.read_text(encoding="utf-8")
    try:
        import yaml
        data = yaml.safe_load(text) or {}
    except Exception:
        data = {}
    return data, text


def _save_capsule_yaml(capsule: Path, data: dict[str, Any], orig_text: str) -> None:
    """将数据写回 capsule.yaml。若有 PyYAML 则 safe_dump 保持结构；若无则通过文本替换。"""
    manifest = capsule / "capsule.yaml"
    import yaml  # 工作区环境自带 PyYAML
    # 针对 PyYAML 序列化时规避 date 类型丢失引号
    new_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    fd, tmp = tempfile.mkstemp(dir=capsule, prefix=".capsule-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp, manifest)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_worklogs(capsule_dir: str | Path) -> list[dict]:
    """宽容读取胶囊内 worklogs 条目。"""
    manifest = Path(capsule_dir) / "capsule.yaml"
    if not manifest.is_file():
        return []
    try:
        import yaml
        d = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        raw = d.get("worklogs") or []
        out = []
        for item in raw:
            if isinstance(item, dict):
                c = dict(item)
                if isinstance(c.get("date"), (date, dt.datetime)):
                    c["date"] = c["date"].strftime("%Y-%m-%d")
                out.append(c)
        return out
    except Exception:
        return []


def read_segments(capsule_dir: str | Path) -> list[dict]:
    """读取胶囊内 task_segments 条目。"""
    manifest = Path(capsule_dir) / "capsule.yaml"
    if not manifest.is_file():
        return []
    try:
        import yaml
        d = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        return [dict(x) for x in (d.get("task_segments") or []) if isinstance(x, dict)]
    except Exception:
        return []


def worklog_total(capsule_dir: str | Path) -> float:
    """胶囊内全部 worklogs 的工时和（有效 spent）。"""
    total = 0.0
    for item in read_worklogs(capsule_dir):
        try:
            total += float(item.get("spent", 0))
        except (TypeError, ValueError):
            continue
    return total


def _read_key(text: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}:[ \t]*([^\s#]*)", text, re.M)
    if not m:
        return None
    v = m.group(1).strip("\"'")
    return None if v in ("", "null", "~") else v


def _set_key(text: str, key: str, value: str) -> str:
    """替换顶层键的值，保留行尾注释；键不存在时追加。"""
    pat = re.compile(rf"^({re.escape(key)}:[ \t]*)[^\s#]*", re.M)
    if pat.search(text):
        return pat.sub(lambda m: m.group(1) + value, text, count=1)
    return text.rstrip("\n") + f"\n{key}: {value}\n"


def _audit_passed(report: Path) -> bool:
    if not report.is_file():
        return False
    try:
        try:
            from .update_audit_state import AuditStateError, load_state
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from update_audit_state import AuditStateError, load_state
        state = load_state(report.read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError, AuditStateError):
        return False
    return not any(i.get("status") == "open" and i.get("level") in BLOCKING for i in state.get("issues", []))


def _filled(text: str, label: str) -> bool:
    m = re.search(rf"\*\*{label}\*\*[:：]\s*(.*)", text)
    return bool(m and m.group(1).strip() and not m.group(1).strip().startswith("待回填"))


def derive(capsule: Path, current: str) -> str:
    """按证据推导应处状态（不含需显式声明的 delivered），只进不退。"""
    target = current
    if current == "draft" and (_audit_passed(capsule / "05_审计报告.md") or any(capsule.glob("04_Spec_*"))):
        target = "active"
    acceptance = capsule / "07_验收报告.md"
    if current == "delivered" and acceptance.is_file():
        text = acceptance.read_text(encoding="utf-8", errors="replace")
        if _filled(text, "数据回收日期"):
            target = "archived"
        elif _filled(text, "未度量兜底说明"):
            target = "archived-unmeasured"
    return target


def update_capsule(capsule_dir: str, *, delivered: str | None = None, dry_run: bool = False) -> dict:
    capsule = Path(capsule_dir).expanduser().resolve()
    manifest = capsule / "capsule.yaml"
    if not manifest.is_file():
        raise ToolError(f"❌ 未找到胶囊清单: {manifest}\n"
                        "👉 修复建议: 传入含 capsule.yaml 的胶囊目录（可用 find_capsule.py 定位）。")
    text = manifest.read_text(encoding="utf-8")
    current = _read_key(text, "lifecycle") or "draft"
    if current not in ORDER:
        raise ToolError(f"❌ lifecycle 取值非法: {current}\n👉 修复建议: 改为 {' | '.join(ORDER)} 之一后重试。")

    target, changes = derive(capsule, current), {}
    if delivered is not None:
        if current in TERMINAL or current == "delivered":
            raise ToolError(f"❌ 胶囊已是 {current}，不能再标记交付\n👉 修复建议: 交付日只写一次；确需更正请人工编辑 capsule.yaml。")
        try:
            day = date.fromisoformat(delivered)
        except ValueError:
            raise ToolError(f"❌ 交付日期格式错误: {delivered}\n👉 修复建议: 使用 YYYY-MM-DD，或省略日期取今天。") from None
        target = "delivered"
        changes.update(delivered_at=day.isoformat(),
                       review_due_at=(day + timedelta(days=REVIEW_DAYS)).isoformat(),
                       closure_deadline=(day + timedelta(days=CLOSURE_DAYS)).isoformat())

    # 方案 3.4 归档门禁检查: 进入 archived 前，胶囊不得有 open 或 pending_approval 段
    if target == "archived":
        segs = read_segments(capsule)
        blockers = [s["seg_id"] for s in segs if s.get("state") in ("open", "pending_approval")]
        if blockers:
            raise ToolError(
                f"❌ 无法归档：胶囊内存在未结任务段: {', '.join(blockers)}\n"
                "👉 修复建议: 请先收尾 open 段，并对待批段执行 --approve-segment 或 --unmeasure-segment 后再归档。"
            )

    # 方案 3.4 archived-unmeasured 自动结转: 遗留的 open / pending_approval 段自动转为 unmeasured
    data, orig_text = _load_capsule_data(capsule)
    if target == "archived-unmeasured":
        segs = data.get("task_segments") or []
        mutated_segs = False
        for s in segs:
            if s.get("state") in ("open", "pending_approval"):
                s["state"] = "unmeasured"
                s["note"] = (s.get("note") or "") + " 随 archived-unmeasured 不计量"
                mutated_segs = True
        if mutated_segs and not dry_run:
            data["task_segments"] = segs
            _save_capsule_yaml(capsule, data, orig_text)

    # 如果有由 delivered 触发自动收尾 open 段
    if delivered is not None:
        segs = data.get("task_segments") or []
        for s in segs:
            if s.get("state") == "open":
                # 自动收尾
                end_task(capsule, trigger="delivered", dry_run=dry_run)
                break

    if target != current:
        changes["lifecycle"] = target

    result = {"capsule": str(capsule), "from": current, "to": target, "changes": changes, "dry_run": dry_run}
    if not changes or dry_run:
        return result
    new = text
    for k, v in changes.items():
        new = _set_key(new, k, v)
    fd, tmp = tempfile.mkstemp(dir=capsule, prefix=".capsule-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
        os.replace(tmp, manifest)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    return result


def _alloc_seq(data: dict[str, Any]) -> tuple[int, str]:
    """分配 seq，校验 seq_next 不小于既有 seq/ref。"""
    seq_next = data.get("seq_next", 1)
    # 提取已有的所有 seq
    used_seqs = []
    for s in data.get("task_segments") or []:
        if isinstance(s, dict) and "seq" in s:
            used_seqs.append(int(s["seq"]))
    for w in data.get("worklogs") or []:
        if isinstance(w, dict) and w.get("ref"):
            m = re.search(r"#(\d+)$", str(w["ref"]))
            if m:
                used_seqs.append(int(m.group(1)))
    max_used = max(used_seqs, default=0)
    if seq_next <= max_used:
        raise ToolError(
            f"❌ 胶囊 seq_next ({seq_next}) 异常，小于等于已有序号 ({max_used})\n"
            "👉 修复建议: 检查 capsule.yaml 中是否存在手动篡改，将 seq_next 调整为大于所有序号的值。"
        )
    allocated = seq_next
    data["seq_next"] = allocated + 1
    return allocated, str(allocated)


def start_task(capsule_dir: str | Path, name: str, task_id: str | None = None, stage: str | None = None,
               at: str | None = None, dry_run: bool = False) -> dict:
    """开启新任务段。"""
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    current = _read_key(text, "lifecycle") or "draft"
    if current in TERMINAL:
        raise ToolError(f"❌ 胶囊已是终态 {current}，不能再开启任务段。")

    segments = data.get("task_segments") or []
    open_segs = [s for s in segments if s.get("state") == "open"]
    if open_segs:
        raise ToolError(
            f"❌ 胶囊已有进行中的任务段: {open_segs[0].get('seg_id')} ({open_segs[0].get('name')})\n"
            "👉 修复建议: 请先收尾进行中的段（--end-task），再开启新任务段。"
        )

    cid = _capsule_id(capsule, text)
    allocated_seq, _ = _alloc_seq(data)
    seg_id = f"{cid}#{allocated_seq}"

    start_dt = normalize_to_cst(at) if at else dt.datetime.now(TZ_CST)
    start_iso = start_dt.isoformat()

    if stage is None:
        stage, _ = _default_stage(capsule)

    seg = {
        "seq": allocated_seq,
        "seg_id": seg_id,
        "name": name,
        "startAt": start_iso,
        "lastActivityAt": start_iso,
        "state": "open",
    }
    if task_id:
        seg["taskId"] = task_id
    if stage:
        seg["stage"] = stage

    errs = _validate_segment(seg)
    if errs:
        raise ToolError(f"❌ 段数据契约校验失败: {'；'.join(errs)}")

    segments.append(seg)
    data["task_segments"] = segments

    if not dry_run:
        _save_capsule_yaml(capsule, data, text)

    return {
        "capsule": str(capsule),
        "status": "started",
        "segment": seg,
        "dry_run": dry_run,
        "worklog_refs": []
    }


def touch_task(capsule_dir: str | Path, at: str | None = None, dry_run: bool = False) -> dict:
    """刷新进行中任务段的 lastActivityAt。"""
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    segments = data.get("task_segments") or []
    open_seg = next((s for s in segments if s.get("state") == "open"), None)
    if not open_seg:
        return {"capsule": str(capsule), "status": "no_open_segment", "dry_run": dry_run}

    act_dt = normalize_to_cst(at) if at else dt.datetime.now(TZ_CST)
    open_seg["lastActivityAt"] = act_dt.isoformat()
    if not dry_run:
        _save_capsule_yaml(capsule, data, text)
    return {"capsule": str(capsule), "status": "touched", "segment": open_seg, "dry_run": dry_run}


def end_task(capsule_dir: str | Path, trigger: str = "deliver", at: str | None = None,
             inferred: bool = False, evidence: str | None = None, note: str | None = None,
             dry_run: bool = False) -> dict:
    """收尾任务段并结算工时。"""
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    current = _read_key(text, "lifecycle") or "draft"
    if current in TERMINAL:
        raise ToolError(f"❌ 胶囊已是终态 {current}，不能再收尾任务段。")

    segments = data.get("task_segments") or []
    open_seg = next((s for s in segments if s.get("state") == "open"), None)
    if not open_seg:
        raise ToolError("❌ 胶囊当前没有进行中的 open 任务段，无需收尾。")

    start_dt = normalize_to_cst(open_seg["startAt"])
    if inferred and not at:
        end_dt = normalize_to_cst(open_seg["lastActivityAt"])
    elif at:
        end_dt = normalize_to_cst(at)
    else:
        end_dt = dt.datetime.now(TZ_CST)

    duration_m = (end_dt - start_dt).total_seconds() / 60.0
    if duration_m <= 0:
        raise ToolError(f"❌ 结束时间早于或等于开始时间: start={start_dt.isoformat()}, end={end_dt.isoformat()}")

    work_date = start_dt.strftime("%Y-%m-%d")
    open_seg["endAt"] = end_dt.isoformat()
    open_seg["lastActivityAt"] = end_dt.isoformat()
    open_seg["trigger"] = trigger
    if inferred:
        open_seg["inferred"] = True
    if evidence:
        open_seg["evidence"] = evidence
    if note:
        open_seg["note"] = note

    worklog_refs = []
    new_worklog = None

    if duration_m <= 480.0:
        # ≤ 8h 自动入账
        open_seg["state"] = "logged"
        spent = q_minutes(duration_m)
        stage = open_seg.get("stage") or _default_stage(capsule)[0]
        operator = _default_operator()
        summary = f"{open_seg['name']}（自动计量入账）"
        new_worklog = {
            "date": work_date,
            "stage": stage,
            "spent": spent,
            "summary": summary[:120],
            "operator": operator,
            "ref": open_seg["seg_id"],
        }
        worklogs = data.get("worklogs") or []
        worklogs.append(new_worklog)
        data["worklogs"] = worklogs
        worklog_refs.append(open_seg["seg_id"])
    elif duration_m <= 1440.0:
        # 8h ~ 24h: 待批准
        open_seg["state"] = "pending_approval"
        open_seg["note"] = (open_seg.get("note") or "") + " 超过 8h"
    else:
        # > 24h: 异常，不可直接批准
        open_seg["state"] = "pending_approval"
        open_seg["note"] = (open_seg.get("note") or "") + " 超过 24h，须拆段"

    if not dry_run:
        _save_capsule_yaml(capsule, data, text)

    return {
        "capsule": str(capsule),
        "status": "ended",
        "segment": open_seg,
        "entry": new_worklog,
        "worklog_refs": worklog_refs,
        "dry_run": dry_run
    }


def approve_segment(capsule_dir: str | Path, seg_id: str, dry_run: bool = False) -> dict:
    """人工批准超过 8h 但不超过 24h 的任务段入账。"""
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    current = _read_key(text, "lifecycle") or "draft"
    if current in TERMINAL:
        raise ToolError(f"❌ 胶囊已是终态 {current}，不能再批准任务段。")

    segments = data.get("task_segments") or []
    target_seg = next((s for s in segments if s.get("seg_id") == seg_id), None)
    if not target_seg:
        raise ToolError(f"❌ 未找到任务段: {seg_id}")
    if target_seg.get("state") != "pending_approval":
        raise ToolError(f"❌ 任务段 {seg_id} 状态为 {target_seg.get('state')}，不是 pending_approval，无需批准。")

    start_dt = normalize_to_cst(target_seg["startAt"])
    end_dt = normalize_to_cst(target_seg["endAt"])
    duration_m = (end_dt - start_dt).total_seconds() / 60.0
    if duration_m > 1440.0:
        raise ToolError(f"❌ 任务段 {seg_id} 原始时长 > 24h ({duration_m:.1f}m)，禁止直接批准；须修正时间或拆段。")

    target_seg["state"] = "logged"
    spent = q_minutes(duration_m)
    stage = target_seg.get("stage") or _default_stage(capsule)[0]
    operator = _default_operator()
    summary = f"{target_seg['name']}（人工批准入账）"
    work_date = start_dt.strftime("%Y-%m-%d")

    entry = {
        "date": work_date,
        "stage": stage,
        "spent": spent,
        "summary": summary[:120],
        "operator": operator,
        "ref": target_seg["seg_id"],
    }
    worklogs = data.get("worklogs") or []
    worklogs.append(entry)
    data["worklogs"] = worklogs

    if not dry_run:
        _save_capsule_yaml(capsule, data, text)

    return {
        "capsule": str(capsule),
        "status": "approved",
        "segment": target_seg,
        "entry": entry,
        "worklog_refs": [seg_id],
        "dry_run": dry_run
    }


def unmeasure_segment(capsule_dir: str | Path, seg_id: str, reason: str, dry_run: bool = False) -> dict:
    """显式将待批段标记为不计量。"""
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    segments = data.get("task_segments") or []
    target_seg = next((s for s in segments if s.get("seg_id") == seg_id), None)
    if not target_seg:
        raise ToolError(f"❌ 未找到任务段: {seg_id}")
    target_seg["state"] = "unmeasured"
    target_seg["note"] = f"不计量: {reason}"
    if not dry_run:
        _save_capsule_yaml(capsule, data, text)
    return {"capsule": str(capsule), "status": "unmeasured", "segment": target_seg, "dry_run": dry_run}


def override_worklog(capsule_dir: str | Path, ref: str, spent: float | None = None,
                     summary: str | None = None, dry_run: bool = False) -> dict:
    """人工覆盖工时条目（原位改写 spent/summary，保留 original_*，置 overridden=True）。"""
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    current = _read_key(text, "lifecycle") or "draft"
    if current in TERMINAL:
        raise ToolError(f"❌ 胶囊已是终态 {current}，不能再覆盖工时。")

    worklogs = data.get("worklogs") or []
    target_log = next((w for w in worklogs if w.get("ref") == ref), None)
    if not target_log:
        raise ToolError(f"❌ 未找到 ref 为 {ref} 的工时条目。")

    if spent is None and summary is None:
        raise ToolError("❌ 必须提供 --spent 或 --summary 进行覆盖。")

    # 首次覆盖保存 original_spent / original_summary
    if not target_log.get("overridden"):
        target_log["overridden"] = True
        target_log["original_spent"] = target_log["spent"]
        target_log["original_summary"] = target_log.get("summary", "")

    if spent is not None:
        if spent <= 0 or spent > 24 or (spent % 0.25) != 0:
            raise ToolError(f"❌ 工时不合法: {spent}（须 > 0、≤ 24 且为 0.25 的倍数）")
        target_log["spent"] = spent
    if summary is not None:
        summary_clean = str(summary).strip()
        if not summary_clean or len(summary_clean) > 120:
            raise ToolError("❌ 摘要不能为空且不得超过 120 字。")
        target_log["summary"] = summary_clean

    errs = _validate_entry(target_log)
    if errs:
        raise ToolError(f"❌ 覆盖后工时条目不合法: {'；'.join(errs)}")

    if not dry_run:
        _save_capsule_yaml(capsule, data, text)

    return {
        "capsule": str(capsule),
        "status": "overridden",
        "entry": target_log,
        "worklog_refs": [ref],
        "dry_run": dry_run,
        "total": worklog_total(capsule)
    }


def log_time(capsule_dir: str | Path, spent: float, stage: str | None = None, summary: str | None = None,
             operator: str | None = None, day: str | None = None, dry_run: bool = False,
             idempotent: bool = False) -> dict:
    """胶囊 worklogs 的唯一人工补登写入入口。"""
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    current = _read_key(text, "lifecycle") or "draft"
    if current in TERMINAL:
        raise ToolError(
            f"❌ 胶囊已是终态 {current}，不能再登记工时\n"
            f"👉 修复建议: 工时只记进行中的事务；如需补记请先把 lifecycle 改回 active。"
        )

    stage_from = "arg"
    if stage is None:
        stage, stage_from = _default_stage(capsule)
    stage = str(stage).strip()
    if not WORKLOG_STAGE_RE.match(stage):
        raise ToolError(f"❌ stage 取值非法: {stage!r}\n👉 修复建议: 形如 02_方案 或 misc。")

    if summary is None or not str(summary).strip():
        if sys.stdin is None or not sys.stdin.isatty():
            raise ToolError("❌ 缺少工时摘要（--summary）\n👉 修复建议: 非交互式环境必须显式给 --summary。")
        summary = input("工时摘要（这段做了什么）: ").strip()
    summary = str(summary).strip()
    if not summary:
        raise ToolError("❌ 工时摘要不能为空。")

    operator = str(operator).strip() if operator else _default_operator()
    if not operator:
        raise ToolError("❌ 无法确定登记人\n👉 修复建议: 先 git config user.name 或用 --operator 显式指定。")

    day = (str(day).strip() if day else date.today().isoformat())
    if not WORKLOG_DATE_RE.match(day):
        raise ToolError(f"❌ 日期格式错误: {day}\n👉 修复建议: 使用 YYYY-MM-DD。")

    try:
        hours = float(spent)
    except (TypeError, ValueError):
        raise ToolError(f"❌ 工时不合法: {spent!r}\n👉 修复建议: 传数字（如 1.5），步长 0.25h。") from None

    # 分配持久 ref
    cid = _capsule_id(capsule, text)
    allocated_seq, _ = _alloc_seq(data)
    ref = f"{cid}#{allocated_seq}"

    entry = {
        "date": day,
        "stage": stage,
        "spent": hours,
        "summary": summary,
        "operator": operator,
        "ref": ref
    }
    errs = _validate_entry(entry)
    if errs:
        raise ToolError(f"❌ 工时条目不符合 schemas/capsule.schema.json: {'；'.join(errs)}")

    existing = data.get("worklogs") or []
    for item in existing:
        match = (
            item.get("date") == entry["date"]
            and item.get("stage") == entry["stage"]
            and abs(float(item.get("spent", 0)) - float(entry["spent"])) < 1e-9
            and item.get("summary") == entry["summary"]
            and item.get("operator") == entry["operator"]
        )
        if match:
            existing_ref = item.get("ref") or ref
            if not idempotent:
                raise ToolError("❌ 该工时已登记过（五元组完全相同）")
            return {
                "capsule": str(capsule),
                "status": "existing",
                "entry": item,
                "stage_from": stage_from,
                "dry_run": dry_run,
                "total": worklog_total(capsule),
                "worklog_refs": [existing_ref]
            }

    existing.append(entry)
    data["worklogs"] = existing

    # 同时刷新当前 open 段的 lastActivityAt（活动事件）
    for s in (data.get("task_segments") or []):
        if s.get("state") == "open":
            s["lastActivityAt"] = dt.datetime.now(TZ_CST).isoformat()
            break

    if not dry_run:
        _save_capsule_yaml(capsule, data, text)

    return {
        "capsule": str(capsule),
        "status": "added",
        "entry": entry,
        "stage_from": stage_from,
        "dry_run": dry_run,
        "total": worklog_total(capsule),
        "worklog_refs": [ref]
    }


def sync_for(path: Path) -> dict | None:
    """供其他工具调用：path 位于某胶囊内时同步其 lifecycle；不在胶囊内或失败均静默返回 None。"""
    for d in [path.resolve().parent, *path.resolve().parents]:
        if (d / "capsule.yaml").is_file():
            try:
                return update_capsule(str(d))
            except (ToolError, OSError):
                return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="胶囊生命周期推进与工时计量：更新 capsule.yaml 的 lifecycle、任务段与 worklogs")
    parser.add_argument("capsule", help="胶囊目录（含 capsule.yaml）")
    parser.add_argument("--delivered", nargs="?", const=date.today().isoformat(), metavar="YYYY-MM-DD",
                        help="标记已交付（代码合入主干且全量测试通过），省略日期取今天")
    parser.add_argument("--log-time", type=float, metavar="HOURS",
                        help="登记工时（小时，0 < spent <= 24，步长 0.25h）")
    parser.add_argument("--stage", help="工时所属阶段（如 02_方案 或 misc）")
    parser.add_argument("--summary", help="工时摘要（做了什么，≤120 字）")
    parser.add_argument("--operator", help="登记人（缺省读 git user.name）")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="工作发生日（缺省今天）")
    parser.add_argument("--idempotent", action="store_true", help="重复记录时静默放行不报错")

    # 段命令
    parser.add_argument("--start-task", action="store_true", help="开始新的任务段")
    parser.add_argument("--name", help="任务段名称")
    parser.add_argument("--task-id", help="任务段关联工程任务 ID")
    parser.add_argument("--at", help="指定开始/结束/活动时间（ISO 8601）")
    parser.add_argument("--end-task", action="store_true", help="收尾当前进行中的任务段")
    parser.add_argument("--trigger", default="deliver", choices=["deliver", "delivered", "uat", "verbal", "switch", "inferred"],
                        help="任务段收尾触发信号类型")
    parser.add_argument("--inferred", action="store_true", help="中断回填标志（缺省以 lastActivityAt 结算）")
    parser.add_argument("--evidence", help="推断或修正证据说明")
    parser.add_argument("--touch", action="store_true", help="刷新进行中任务段的 lastActivityAt")
    parser.add_argument("--approve-segment", metavar="SEG_ID", help="人工批准 >8h 且 ≤24h 的任务段入账")
    parser.add_argument("--unmeasure-segment", metavar="SEG_ID", help="将待批段标记为不计量")
    parser.add_argument("--reason", help="不计量原因")
    parser.add_argument("--override", metavar="REF", help="人工覆盖指定 ref 的工时条目")
    parser.add_argument("--spent", type=float, help="覆盖后的工时数")

    parser.add_argument("--dry-run", action="store_true", help="只输出将要做的变更，不写入")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    try:
        res = None
        if args.start_task:
            if not args.name:
                raise ToolError("❌ --start-task 必须指定 --name <段名>")
            res = start_task(args.capsule, name=args.name, task_id=args.task_id, stage=args.stage,
                             at=args.at, dry_run=args.dry_run)
        elif args.touch:
            res = touch_task(args.capsule, at=args.at, dry_run=args.dry_run)
        elif args.end_task:
            res = end_task(args.capsule, trigger=args.trigger, at=args.at, inferred=args.inferred,
                           evidence=args.evidence, note=args.reason, dry_run=args.dry_run)
        elif args.approve_segment:
            res = approve_segment(args.capsule, seg_id=args.approve_segment, dry_run=args.dry_run)
        elif args.unmeasure_segment:
            if not args.reason:
                raise ToolError("❌ --unmeasure-segment 必须指定 --reason <原因>")
            res = unmeasure_segment(args.capsule, seg_id=args.unmeasure_segment, reason=args.reason, dry_run=args.dry_run)
        elif args.override:
            res = override_worklog(args.capsule, ref=args.override, spent=args.spent, summary=args.summary, dry_run=args.dry_run)
        elif args.log_time is not None:
            res = log_time(args.capsule, spent=args.log_time, stage=args.stage, summary=args.summary,
                           operator=args.operator, day=args.date, dry_run=args.dry_run, idempotent=args.idempotent)
        else:
            res = update_capsule(args.capsule, delivered=args.delivered, dry_run=args.dry_run)
    except ToolError as err:
        print(str(err), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        prefix = "[dry-run] " if args.dry_run else ""
        status = res.get("status")
        if status == "started":
            print(f"{prefix}已开启任务段: {res['segment']['seg_id']} ({res['segment']['name']})")
        elif status == "ended":
            seg = res["segment"]
            ent = res.get("entry")
            if ent:
                print(f"{prefix}任务段收尾并入账: {seg['seg_id']} [{seg['state']}] {ent['spent']}h | ref: {ent.get('ref')}")
            else:
                print(f"{prefix}任务段收尾: {seg['seg_id']} [{seg['state']}] ({seg.get('note') or '待审批'})")
        elif status == "approved":
            print(f"{prefix}任务段批准入账: {res['segment']['seg_id']} -> {res['entry']['spent']}h")
        elif status == "overridden":
            print(f"{prefix}已覆盖工时: {res['entry']['ref']} -> {res['entry']['spent']}h")
        elif status == "touched":
            print(f"{prefix}已刷新任务段活跃时间: {res.get('segment', {}).get('seg_id')}")
        elif status in ("added", "existing"):
            ent = res["entry"]
            print(f"{prefix}已登记工时: {ent['date']} [{ent['stage']}] {ent['spent']}h - {ent['summary']} ({ent['operator']}) | 累计: {res['total']}h")
        else:
            changes = res.get("changes", {})
            if changes:
                print(f"{prefix}lifecycle {res['from']} → {res['to']} " + " ".join(f"{k}={v}" for k, v in changes.items() if k != "lifecycle"))
            else:
                print(f"lifecycle={res['from']} 无需变更")
    return 0


if __name__ == "__main__":
    sys.exit(main())
