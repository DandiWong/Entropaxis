#!/usr/bin/env python3
"""
胶囊生命周期推进：按胶囊内已有证据把 capsule.yaml 的 lifecycle 及交付日期往前推。

状态只进不退，终态（archived / archived-unmeasured）不再改动：
  draft    → active               03_方案审计报告.md（旧名 05_审计报告.md）无 open 的 Critical/Major，或已有 04_Spec_*
  active   → delivered            仅显式 --delivered [YYYY-MM-DD]（"合入主干且全量测试通过"无法从胶囊文件推断）
                                  同时写 delivered_at，派生 review_due_at = +30 天、closure_deadline = +60 天
  delivered→ archived             07_验收报告.md「数据回收日期」已回填
  delivered→ archived-unmeasured  07_验收报告.md「未度量兜底说明」已回填（且数据回收日期未回填）

只改 capsule.yaml 中对应的顶层键值，保留注释与其余内容；写入走临时文件后原子替换。
调用方：update_audit_state.py 写报告后自动同步；《软件工程》「收尾分支」合入后显式 --delivered；
lint_workspace.py 用 derive() 检查状态落后。

段计量（Tech-121，方案 §3.2～3.4、Spec §二）：
  任务段是事实层，worklogs 是唯一计量真源；一次 CLI 调用内的段变化、worklogs 追加与
  lifecycle 推进在同一份数据对象上完成，最终只做一次原子写入（IMP-C1：不再用旧全文
  覆盖已落盘的段结算）。
  >24h 异常段只能修正结束时间（--fix-segment）或按时刻拆段（--split-segment），
  不提供直接批准路径（IMP-M1）。
  logged 是终态：--unmeasure-segment 只接受 open / pending_approval 源状态（IMP-C3，
  维护 I-3「logged 段与 worklogs.ref 一一对应」）。
  每次写盘前统一执行全局契约与不变量校验：seq_next、logged 段与 ref 一一对应、ref 唯一、
  段状态与 endAt/worklog 的对应关系（IMP-M6）。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
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

# 段状态机（schemas/capsule.schema.json 的 task_segments 条目）。
SEGMENT_STATES = ("open", "pending_approval", "logged", "unmeasured")
# 允许显式标记为不计量的源状态（IMP-C3）：logged 已有 worklog，撤段会破坏 I-3。
UNMEASURABLE_SOURCES = ("open", "pending_approval")
# 时长门槛按整数秒判定（方案 §4.4 判定 1、§4.4.2）：≤8h 自动入账；8h～24h 待人工批准；
# >24h 只能修正或拆段。判定不取整：8h01m（28860s）不得被归成 8h 而绕过待批。
AUTO_LIMIT_S = 28800
APPROVAL_LIMIT_S = 86400
# Manager 段尾部空档阈值（方案 4.4 判定 7、D-3）：最后一次工具事件之后超过 600 秒
# 视为空档，结束时刻取最后一次事件；不超过则取 now。
IDLE_GAP_S = 600
# 胶囊写锁：所有写函数在「读→改→写」全程持有 `.capsule.yaml.lock` 目录锁，
# 复用 dispatch_role.ManifestStore 的过期接管逻辑，最长等待 10 秒（Tech-176 T6）。
CAPSULE_LOCK_WAIT_S = 10.0


class ToolError(Exception):
    """可恢复业务异常，包含行动导向修复指引。"""


def _yaml_str(value: str) -> str:
    """自由文本 → JSON 双引号标量（JSON 字符串语法是 YAML 双引号标量的严格子集）。"""
    return json.dumps(value, ensure_ascii=False)


def normalize_to_cst(iso_str: str) -> dt.datetime:
    """归一化到 +08:00 时区。"""
    try:
        d = dt.datetime.fromisoformat(iso_str)
    except Exception as exc:
        raise ToolError(f"❌ 时间格式不合法: {iso_str}（须为 ISO 8601，如 2026-10-15T10:00:00+08:00）") from exc
    if d.tzinfo is None:
        return d.replace(tzinfo=TZ_CST)
    return d.astimezone(TZ_CST)

# 工时单位换算（Tech-176 T2）：真源是整数秒，spent 由它派生。
SECONDS_PER_HOUR = 3600
MAX_WORKLOG_SECONDS = 86400  # 与 schema 的 seconds/spent 上限一致（24h）
_DUR_HMS_RE = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)$")
_DUR_COMBO_RE = re.compile(r"^(\d+(?:\.\d+)?h)?(\d+(?:\.\d+)?m)?(\d+(?:\.\d+)?s)?$", re.IGNORECASE)


def parse_duration(text: str) -> int:
    """时长文本 → 整数秒（Tech-176 T2，方案 4.4 判定 1）。

    接受三种写法：纯数字按小时（兼容 --log-time 1.5）；h/m/s 任意组合（1h23m、45m、30s）；
    H:MM:SS（01:23:45）。结果必须落在 1..86400 秒，否则 ToolError（"0"、"-1"、"1x"、"25h" 均报错）。
    """
    s = str(text).strip()
    if not s:
        raise ToolError("❌ 时长不能为空\n👉 修复建议: 传 1.5（小时）、1h23m 或 01:23:45。")
    m = _DUR_HMS_RE.match(s)
    if m:
        seconds = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    elif re.fullmatch(r"\d+(?:\.\d+)?", s):
        seconds = int(round(float(s) * SECONDS_PER_HOUR))
    else:
        m = _DUR_COMBO_RE.match(s)
        if not m or not any(m.groups()):
            raise ToolError(f"❌ 时长格式不合法: {text!r}\n👉 修复建议: 传 1.5（小时）、1h23m 或 01:23:45。")
        h, mi, sec = (float(x.rstrip("hms")) if x else 0.0 for x in m.groups())
        seconds = int(round(h * 3600 + mi * 60 + sec))
    if seconds < 1 or seconds > MAX_WORKLOG_SECONDS:
        raise ToolError(
            f"❌ 时长超出范围: {text!r}（换算 {seconds}s，须 1..{MAX_WORKLOG_SECONDS}s，即 >0 且 ≤24h）"
        )
    return seconds


def fmt_hms(seconds: int) -> str:
    """整数秒 → H:MM:SS（时不过零填充，分秒两位）：5025 → '1:23:45'。"""
    seconds = int(seconds)
    if seconds < 0:
        raise ToolError(f"❌ 秒数不能为负: {seconds}")
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _seconds_to_spent(seconds: int) -> float:
    """整数秒 → 小时（派生 spent 的唯一定义）：round(seconds / 3600, 4)。"""
    return round(seconds / SECONDS_PER_HOUR, 4)


def _import_validate_schema():
    try:
        from . import validate_schema
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import validate_schema
    return validate_schema


def _capsule_schema() -> dict:
    try:
        validate_schema = _import_validate_schema()
        return validate_schema.load_schema("capsule")
    except (ImportError, OSError, ValueError) as exc:
        raise ToolError(
            f"❌ 无法读取契约 schemas/capsule.schema.json: {exc}\n"
            "👉 修复建议: 确认 .entropaxis/schemas/capsule.schema.json 存在且为合法 JSON。"
        ) from exc


def _validate_entry(entry: dict) -> list[str]:
    """用 capsule.schema.json 的 worklogs 条目契约校验单条记录；空列表表示通过。"""
    return _import_validate_schema().validate(entry, _capsule_schema()["properties"]["worklogs"]["items"])


def _validate_segment(segment: dict) -> list[str]:
    """用 capsule.schema.json 的 task_segments 条目契约校验单条记录；空列表表示通过。"""
    return _import_validate_schema().validate(segment, _capsule_schema()["properties"]["task_segments"]["items"])


def _default_operator() -> str:
    """登记人缺省取 git config user.name，再退环境变量；都取不到即报错，不写空值。"""
    proc = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
    name = proc.stdout.strip() if proc.returncode == 0 else ""
    return name or os.environ.get("USER") or os.environ.get("USERNAME") or ""


def _capsule_id(capsule: Path, text: str) -> str:
    cid = _read_key(text, "id")
    if cid:
        return cid
    m = re.search(r"^\d{8}_(.*)$", capsule.name)
    return m.group(1) if m else capsule.name


@contextlib.contextmanager
def _capsule_lock(capsule: Path):
    """持有 `.capsule.yaml.lock` 目录锁的上下文管理器（Tech-176 T6，方案 4.4 判定 6）。

    复用 dispatch_role.ManifestStore 的目录锁：锁目录同名 `.capsule.yaml.lock`，
    lease 期内 mkdir 失败即视为占用，过期锁由 ManifestStore 按接管逻辑恢复（归档
    而不静默删除）。所有写函数在「读→改→写」全程持锁，最长等待 10 秒，超时报
    ToolError 且不落盘。

    不重入：调用方不得在已持有 ManifestStore 锁时再取本锁。现有调用顺序已满足
    ——`run_assignment_result` 的段操作（T9 起走 `start/end_dispatch_segment`）在
    ManifestStore 释放之后，`execute_chain` 的 `sync_for`（:875）在 ManifestStore
    获取之前。T10 接线时保持该顺序。
    """
    store = _manifest_store(capsule / "capsule.yaml")
    owner = f"update_capsule:{os.getpid()}"
    deadline = time.monotonic() + CAPSULE_LOCK_WAIT_S
    while not store.acquire(owner):
        if time.monotonic() >= deadline:
            raise ToolError(
                f"❌ 胶囊写锁等待超时（{CAPSULE_LOCK_WAIT_S:g}s）: {store.lock_dir}\n"
                "👉 修复建议: 确认没有并发的胶囊写操作后重试；"
                f"锁租约 {_lease_hint(store)}s 内会自动过期。"
            )
        time.sleep(0.05)
    try:
        yield store
    finally:
        store.release()


def _lease_hint(store: Any) -> int:
    """读 owner.json 的剩余租约秒数，只用于提示语；读不到退 600（ManifestStore 默认租约）。"""
    try:
        meta = json.loads((store.lock_dir / "owner.json").read_text(encoding="utf-8"))
        lease = dt.datetime.fromisoformat(meta["lease_until"])
        if lease.tzinfo is None:
            lease = lease.replace(tzinfo=dt.timezone.utc)
        return max(0, int((lease - dt.datetime.now(dt.timezone.utc)).total_seconds()))
    except (OSError, ValueError, KeyError, TypeError):
        return 600


def _manifest_store(manifest: Path) -> Any:
    """延迟导入 dispatch_role.ManifestStore 复用其目录锁（T6）；导入失败即报可行动错误。"""
    try:
        from . import dispatch_role
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            import dispatch_role  # type: ignore
        except ImportError as exc:
            raise ToolError(
                "❌ 无法导入 dispatch_role.ManifestStore（胶囊写锁依赖其目录锁）\n"
                "👉 修复建议: 确认 tools/dispatch_role.py 与本工具同目录且可导入。"
            ) from exc
    return dispatch_role.ManifestStore(manifest)


def _locked(fn):
    """把公开写函数的「读→改→写」全程纳入胶囊目录锁（Tech-176 T6，方案 4.4 判定 6）。

    并发写同一 capsule.yaml 时，两个进程都会各自读到旧全文再整份回写，后写者
    静默丢更新（AC-10）。锁在读之前取、写之后放；超时报 ToolError 且不落盘。
    无 capsule.yaml 时不取锁，交给被装饰函数报「未找到胶囊清单」。
    """
    @functools.wraps(fn)
    def wrapper(capsule_dir: str | Path, *args: Any, **kwargs: Any):
        capsule = Path(capsule_dir).expanduser().resolve()
        if not (capsule / "capsule.yaml").is_file():
            return fn(capsule_dir, *args, **kwargs)
        with _capsule_lock(capsule):
            return fn(capsule_dir, *args, **kwargs)
    return wrapper


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


def _save_capsule_yaml(capsule: Path, data: dict[str, Any], orig_text: str,
                       activity_at: str | None = None) -> None:
    """将数据写回 capsule.yaml。若有 PyYAML 则 safe_dump 保持结构；若无则通过文本替换。

    任何写胶囊的动作都是一次 Entropaxis 工具事件（方案 4.4 判定 7「7 工具事件」、
    第六节「只靠 Entropaxis 工具事件」）：本胶囊若有 open 的 Manager 段（或旧段），
    同步刷新它的 lastActivityAt。放在这里而不是各写函数里，是因为「调度器写胶囊」
    这类真实事件发生在 dispatch_role 的 manifest 写回路径上，单独一个个接必然漏。
    open 的 role 段不刷新——它的时间戳只由调度自己写（T9/T10）。
    `activity_at` 给显式时刻（`--touch --at`），缺省取 now：显式回填时刻不得被 now 覆盖。
    """
    _touch_open_segment(data, activity_at)
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


def _entry_seconds(entry: dict) -> int:
    """worklog 有效秒：有 `seconds`（整数真源）用它，旧条目按 round(spent×3600) 补算。

    方案 4.4 判定 10、五「新旧条目混存的汇总口径」：旧条目不改写、不推断 actor。
    """
    seconds = entry.get("seconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        return max(0, round(float(seconds)))
    try:
        return max(0, round(float(entry.get("spent", 0)) * 3600))
    except (TypeError, ValueError):
        return 0


def worklog_total(capsule_dir: str | Path) -> float:
    """胶囊内全部 worklogs 的工时和（有效 spent，由 seconds 真源派生）。

    Tech-176 T13（方案 4.4 判定「下游」）：先按秒求和再换算小时，不累加浮点 spent。
    """
    return _seconds_to_spent(sum(_entry_seconds(item) for item in read_worklogs(capsule_dir)))


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
    plan_audit = capsule / "03_方案审计报告.md"
    if not plan_audit.is_file():
        plan_audit = capsule / "05_审计报告.md"  # 旧胶囊：方案与实施合在一份报告
    if current == "draft" and (_audit_passed(plan_audit) or any(capsule.glob("04_Spec_*"))):
        target = "active"
    acceptance = capsule / "07_验收报告.md"
    if current == "delivered" and acceptance.is_file():
        text = acceptance.read_text(encoding="utf-8", errors="replace")
        if _filled(text, "数据回收日期"):
            target = "archived"
        elif _filled(text, "未度量兜底说明"):
            target = "archived-unmeasured"
    return target


# ---------------------------------------------------------------------------
# 写前全局契约与不变量校验（IMP-M6）
# ---------------------------------------------------------------------------

def _invariant_errors(data: dict[str, Any]) -> list[str]:
    """写盘前统一校验跨集合不变量；空列表表示通过。

    覆盖 I-3（logged 段与 worklogs.ref 一一对应）、ref 唯一性、seq_next 单调性、
    段状态与 endAt / worklog 的对应关系，以及秒级化契约的 I-7（spent 与 seconds 一致）、
    I-8（actor=role 必带 role）、I-10（入账 actor 段必带 effective_seconds，manager 段
    另需 evidence_sha256）。schema 只能表达单条目契约，这些跨集合条件必须在这里拦，
    否则手改或半成品状态会被后续写入继承。
    """
    errs: list[str] = []
    segments = [s for s in (data.get("task_segments") or []) if isinstance(s, dict)]
    worklogs = [w for w in (data.get("worklogs") or []) if isinstance(w, dict)]

    # 1) seq_next 必须大于所有已用序号（段 seq 与 worklog ref 尾部序号共用同一计数器）。
    used: set[int] = set()
    for s in segments:
        try:
            used.add(int(s.get("seq")))
        except (TypeError, ValueError):
            errs.append(f"段 {s.get('seg_id')} 的 seq 非法: {s.get('seq')!r}")
    for w in worklogs:
        ref = str(w.get("ref") or "")
        m = re.search(r"#(\d+)$", ref)
        if m:
            used.add(int(m.group(1)))
    seq_next = data.get("seq_next", 1)
    if not isinstance(seq_next, int) or isinstance(seq_next, bool) or seq_next < 1:
        errs.append(f"seq_next 非法: {seq_next!r}（须为 ≥1 的整数）")
    elif used and seq_next <= max(used):
        errs.append(
            f"seq_next ({seq_next}) 小于等于已用最大序号 ({max(used)})；"
            "检查 capsule.yaml 是否被手工篡改"
        )

    # 2) ref 唯一。
    seen_refs: set[str] = set()
    for w in worklogs:
        ref = str(w.get("ref") or "")
        if not ref:
            continue
        if ref in seen_refs:
            errs.append(f"worklog ref 重复: {ref}")
        seen_refs.add(ref)

    # 3) I-3：logged 段与 worklogs.ref 的映射关系。
    # 方案 §3.1 I-3/I-4：
    #   - 每一个 logged 段恰有一条 worklog 的 ref 等于该段 seg_id；
    #   - 非 logged 段（open/pending_approval/unmeasured）严禁存在对应 worklog（反向 I-3 校验）；
    #   - 人工补登（--log-time）生成的条目有自己的 ref，但没有对应段（I-4）。
    logged_ids = [str(s.get("seg_id")) for s in segments if s.get("state") == "logged"]
    dup_logged = {i for i in logged_ids if logged_ids.count(i) > 1}
    for seg_id in sorted(dup_logged):
        errs.append(f"logged 段 seg_id 重复: {seg_id}")
    for seg_id in logged_ids:
        if seg_id not in seen_refs:
            errs.append(f"logged 段 {seg_id} 缺少对应 worklog（I-3 不变量被破坏）")

    non_logged_ids = {str(s.get("seg_id")) for s in segments if s.get("state") != "logged"}
    for ref in sorted(seen_refs):
        if ref in non_logged_ids:
            errs.append(f"非 logged 段 {ref} 存在对应 worklog（反向 I-3 不变量被破坏）")

    # 4) 段状态与 endAt 对应：logged 必须已收尾。
    for s in segments:
        if s.get("state") == "logged" and not s.get("endAt"):
            errs.append(f"logged 段 {s.get('seg_id')} 缺少 endAt")

    # 5) I-7：带 seconds 的条目必须满足 spent == round(seconds/3600, 4)。
    #    手改或半成品状态不得被后续写入继承（方案 4.3.1）。
    for w in worklogs:
        seconds = w.get("seconds")
        if seconds is None:
            continue
        try:
            seconds_i = int(seconds)
            expected = _seconds_to_spent(seconds_i)
            actual = float(w.get("spent"))
        except (TypeError, ValueError):
            errs.append(f"worklog {w.get('ref')} 的 seconds/spent 无法比较: {seconds!r} / {w.get('spent')!r}")
            continue
        if abs(actual - expected) >= 1e-9:
            errs.append(
                f"worklog {w.get('ref')} 的 spent ({actual}) 与 seconds ({seconds_i}) 不一致"
                f"（I-7：应为 {expected}）"
            )

    # 6) I-8：actor=role 的条目与段必须带 role。
    for w in worklogs:
        if w.get("actor") == "role" and not w.get("role"):
            errs.append(f"worklog {w.get('ref')} 的 actor=role 但缺少 role（I-8 不变量被破坏）")
    for s in segments:
        if s.get("actor") == "role" and not s.get("role"):
            errs.append(f"段 {s.get('seg_id')} 的 actor=role 但缺少 role（I-8 不变量被破坏）")

    # 8) I-9：同一胶囊内 dispatch_id 唯一（角色段按 dispatch_id 结段的前提）。
    seen_dispatch: set[str] = set()
    for s in segments:
        did = s.get("dispatch_id")
        if not did:
            continue
        if did in seen_dispatch:
            errs.append(f"dispatch_id 重复: {did}（I-9 不变量被破坏）")
        seen_dispatch.add(did)

    # 7) I-10：actor 段一旦入账（logged / pending_approval）必须有 effective_seconds；
    #    actor=manager 的这类段还必须有 evidence_sha256（方案 4.4.1 兜底）。
    for s in segments:
        if s.get("actor") not in ("role", "manager"):
            continue
        if s.get("state") not in ("logged", "pending_approval"):
            continue
        if s.get("effective_seconds") is None:
            errs.append(
                f"段 {s.get('seg_id')} 的 actor={s.get('actor')} 且 state={s.get('state')} "
                "但缺少 effective_seconds（I-10 不变量被破坏）"
            )
        if s.get("actor") == "manager" and not s.get("evidence_sha256"):
            errs.append(
                f"段 {s.get('seg_id')} 的 actor=manager 且 state={s.get('state')} "
                "但缺少 evidence_sha256（I-10 不变量被破坏）"
            )
    return errs


def _check_invariants(data: dict[str, Any]) -> None:
    """IMP-M6：任一不变量违规即阻断写入，磁盘字节不变。"""
    errs = _invariant_errors(data)
    if errs:
        raise ToolError(
            "❌ 胶囊数据违反全局不变量，拒绝写入:\n  - " + "\n  - ".join(errs) + "\n"
            "👉 修复建议: 修正 capsule.yaml 中对应字段（seq_next / task_segments / worklogs）后重试。"
        )


def _now_iso() -> str:
    return dt.datetime.now(TZ_CST).isoformat()


def _find_open_segment(segments: list[dict]) -> dict | None:
    return next((s for s in segments if isinstance(s, dict) and s.get("state") == "open"), None)


# 全部结算入口的 actor 政策（方案 4.4.1，PA-1）。入口按 actor 分支：
#   旧段（无 actor）——沿用旧语义（墙钟秒、不查证据）；
#   actor=manager ——证据门槛不可绕过：没有 `--end-task --evidence` 的入口一律转 unmeasured；
#   actor=role ——只由调度按 dispatch_id 结段，其它入口不碰、拒绝结算。
ROLE_SEGMENT_REFUSAL = (
    "👉 修复建议: 角色段由 dispatch_role.py run 自开自结；"
    "残留段请用 --unmeasure-segment {seg_id} --reason <原因> 收尾。"
)


def _refuse_role_segment(entry: str, seg: dict) -> ToolError:
    """4.4.1：除 `--unmeasure-segment` 与归档自动结转外，任何入口不结算 role 段。"""
    return ToolError(
        f"❌ {entry}不接受角色调度段（actor=role）: {seg.get('seg_id')}\n"
        + ROLE_SEGMENT_REFUSAL.format(seg_id=seg.get("seg_id"))
    )


def _append_note(existing: str | None, reason: str) -> str:
    """段 note 追加：原 note 为空时不以空格开头（验收 O-3），非空时用单空格分隔。"""
    return f"{existing or ''} {reason}".strip()


def _mark_unmeasured_without_settling(data: dict[str, Any], seg: dict, trigger: str,
                                      reason: str) -> None:
    """4.4.1：不绕过证据门槛的入口把 actor 段转 unmeasured，不写 worklog（D-14）。"""
    seg["state"] = "unmeasured"
    seg["trigger"] = trigger
    seg["endAt"] = seg.get("endAt") or _now_iso()
    seg["lastActivityAt"] = seg["endAt"]
    seg["note"] = _append_note(seg.get("note"), reason)


def _touch_open_segment(data: dict[str, Any], at: str | None = None) -> None:
    """统一活动刷新路径（方案 §3.2 活动生产者，Tech-176 T7 判定 7 末行）。

    只刷新本胶囊 open 的 Manager 段或旧段（无 actor）的 lastActivityAt：open 的
    role 段由调度自己刷新（T9 的 `end_dispatch_segment` 前显式 touch），Manager
    工具事件不去改它的时间戳，否则会把角色执行时长算进 Manager 段。
    """
    seg = next((s for s in (data.get("task_segments") or [])
                if isinstance(s, dict) and s.get("state") == "open"
                and s.get("actor") in (None, "manager")), None)
    if seg is not None:
        seg["lastActivityAt"] = normalize_to_cst(at).isoformat() if at else _now_iso()


def _segment_duration_seconds(seg: dict, *, default_end: dt.datetime | None = None) -> int:
    """段墙钟秒数 = max(1, round(endAt − startAt))（方案 4.4.2 machine_seconds）。"""
    start_dt = normalize_to_cst(seg["startAt"])
    if seg.get("endAt"):
        end_dt = normalize_to_cst(seg["endAt"])
    elif default_end is not None:
        end_dt = default_end
    else:
        end_dt = dt.datetime.now(TZ_CST)
    raw = (end_dt - start_dt).total_seconds()
    if raw <= 0:
        raise ToolError(
            f"❌ 结束时间早于或等于开始时间: start={start_dt.isoformat()}, end={end_dt.isoformat()}"
        )
    return max(1, round(raw))


def _close_segment(capsule: Path, data: dict[str, Any], seg: dict, end_dt: dt.datetime,
                   trigger: str) -> dict | None:
    """给未收尾段补 endAt/lastActivityAt 并按 §4.4.2 结算，返回新 worklog（若有）。"""
    start_dt = normalize_to_cst(seg["startAt"])
    seg["endAt"] = end_dt.isoformat()
    seg["lastActivityAt"] = end_dt.isoformat()
    seg["trigger"] = trigger
    return _settle_segment(data, seg, max(1, round((end_dt - start_dt).total_seconds())))


def _settle_segment(data: dict[str, Any], seg: dict, seconds: int,
                    machine_seconds: int | None = None) -> dict | None:
    """按整数秒把已收尾段结算为 logged / pending_approval，返回新 worklog（若有）。

    门槛按原始秒数判定（方案 4.4.2）：28860 秒（8h01m）不得被归成 8h 而绕过待批。
    段写 machine_seconds；旧段（无 actor）的 effective_seconds 不写，worklog seconds
    等于 machine_seconds（4.4.2 末行）。spent 由 seconds 派生（round(秒/3600, 4)）。
    角色/Manager 段由 T8/T9 先写 effective_seconds 与各自的 machine_seconds，本函数
    只按门槛判定状态并生成 worklog。
    """
    seconds = max(1, round(seconds))
    if machine_seconds is not None:
        seg["machine_seconds"] = max(1, round(machine_seconds))
    else:
        seg["machine_seconds"] = seconds
    actor = seg.get("actor")
    effective = seg.get("effective_seconds")
    effective = max(0, round(effective)) if isinstance(effective, (int, float)) else seconds
    if seconds <= AUTO_LIMIT_S:
        seg["state"] = "logged"
        stage = seg.get("stage") or "misc"
        operator = _default_operator()
        entry: dict[str, Any] = {
            "date": normalize_to_cst(seg["startAt"]).strftime("%Y-%m-%d"),
            "stage": stage,
            "spent": _seconds_to_spent(effective),
            "seconds": effective,
            "summary": f"{seg['name']}（自动计量入账）"[:120],
            "operator": operator,
            "ref": seg["seg_id"],
        }
        if actor in ("role", "manager"):
            entry["actor"] = actor
            if actor == "role" and seg.get("role"):
                entry["role"] = seg["role"]
        worklogs = data.get("worklogs") or []
        worklogs.append(entry)
        data["worklogs"] = worklogs
        return entry
    if seconds <= APPROVAL_LIMIT_S:
        seg["state"] = "pending_approval"
        seg["note"] = _append_note(seg.get("note"), f"超过 8h（{fmt_hms(seconds)}）")
        return None
    seg["state"] = "pending_approval"
    seg["note"] = _append_note(seg.get("note"), f"超过 24h，须拆段（{fmt_hms(seconds)}）")
    return None


@_locked  # T6：读→改→写全程持胶囊目录锁
def update_capsule(capsule_dir: str, *, delivered: str | None = None, dry_run: bool = False) -> dict:
    """推进 lifecycle。段结算、worklogs 追加与 lifecycle 变化在同一数据对象上完成后一次原子写入（IMP-C1）。"""
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

    data, orig_text = _load_capsule_data(capsule)

    # 方案 3.4 归档门禁检查: 进入 archived 前，胶囊不得有 open 或 pending_approval 段
    if target == "archived":
        segs = data.get("task_segments") or []
        blockers = [s["seg_id"] for s in segs if s.get("state") in ("open", "pending_approval")]
        if blockers:
            raise ToolError(
                f"❌ 无法归档：胶囊内存在未结任务段: {', '.join(blockers)}\n"
                "👉 修复建议: 请先收尾 open 段，并对待批段执行 --approve-segment 或 --unmeasure-segment 后再归档。"
            )

    worklog_refs: list[str] = []
    # 本调用是否改动了段/worklog：有改动时必须连同 lifecycle 一起写整份数据对象，
    # 绝不能用旧全文覆盖已落盘的段结算（IMP-C1）。
    mutated = False

    # --delivered 是一个收尾信号：在同一数据对象内自动收尾 open 段（IMP-C1）。
    # 方案 4.4.1（PA-1）：open 的 Manager 段转 unmeasured 不入账（D-14，交付时没带证据
    # 结段，宁可不计）；open 的 role 段不碰（属运行中或残留的调度，按第六节处置）；
    # 旧段（无 actor）沿用现行语义按秒结算。
    if delivered is not None:
        open_seg = _find_open_segment([s for s in (data.get("task_segments") or []) if isinstance(s, dict)])
        if open_seg is not None:
            seg_actor = open_seg.get("actor")
            if seg_actor == "role":
                pass  # 不碰：交付照常推进，段状态与 worklog 都不动
            elif seg_actor == "manager":
                _mark_unmeasured_without_settling(
                    data, open_seg, "delivered", "--delivered 收尾：未带证据结段，段作废不入账")
                mutated = True
            else:
                end_dt = normalize_to_cst(open_seg["endAt"]) if open_seg.get("endAt") else dt.datetime.now(TZ_CST)
                entry = _close_segment(capsule, data, open_seg, end_dt, "delivered")
                if entry is not None:
                    worklog_refs.append(open_seg["seg_id"])
                mutated = True

        # 交付终审时：自动捕获外部依赖的当前 SHA256 指纹，加固防腐封箱
        deps = data.get("dependencies")
        if isinstance(deps, list) and deps:
            new_deps = []
            for d in deps:
                is_str = isinstance(d, str)
                raw_p = d if is_str else (d.get("path") if isinstance(d, dict) else None)
                if not raw_p:
                    new_deps.append(d)
                    continue
                t_path = (capsule / raw_p).resolve()
                if not t_path.exists():
                    t_path = (paths.WORKSPACE_ROOT / raw_p).resolve()
                if t_path.is_file():
                    sha = hashlib.sha256(t_path.read_bytes()).hexdigest()
                    item_obj = {"path": raw_p, "role": "living_asset", "sha256": sha, "captured_at": _now_iso()} if is_str else dict(d)
                    if not is_str:
                        item_obj["sha256"] = sha
                        item_obj["captured_at"] = _now_iso()
                    new_deps.append(item_obj)
                else:
                    new_deps.append(d)
            data["dependencies"] = new_deps
            mutated = True

    # 方案 3.4 archived-unmeasured 自动结转: 遗留的 open / pending_approval 段自动转为 unmeasured。
    # 只在 lifecycle 推进到 archived-unmeasured 时结转；`--delivered` 不触发（4.4.1 的
    # actor 政策已分别处理 Manager / role / 旧段）。
    if target == "archived-unmeasured":
        for s in data.get("task_segments") or []:
            if isinstance(s, dict) and s.get("state") in ("open", "pending_approval"):
                s["state"] = "unmeasured"
                s["note"] = _append_note(s.get("note"), "随 archived-unmeasured 不计量")
                mutated = True

    if target != current:
        changes["lifecycle"] = target

    result = {"capsule": str(capsule), "from": current, "to": target, "changes": changes,
              "dry_run": dry_run, "worklog_refs": worklog_refs}
    if not changes or dry_run:
        return result

    if mutated:
        # 唯一一次写盘：段变化、worklogs 与 lifecycle 一并原子落盘。
        data["lifecycle"] = target
        for key, value in changes.items():
            if key != "lifecycle":
                data[key] = value
        _check_invariants(data)
        _save_capsule_yaml(capsule, data, orig_text)
        return result

    # 纯 lifecycle 推进：保留注释与其余内容的定点替换。
    # IMP-M6：写前全局不变量校验，手改/损坏状态不得被新写入继承。
    _check_invariants(data)
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


@_locked  # T6：读→改→写全程持胶囊目录锁
def start_task(capsule_dir: str | Path, name: str, task_id: str | None = None, stage: str | None = None,
               at: str | None = None, dry_run: bool = False) -> dict:
    """开启 Manager 任务段（Tech-176 T7，方案 4.4 判定 7）。

    只开 `actor: manager` 段：角色段由调度自开自结（T9），本入口不再承担角色段。
    开段即记录 `baseline`——胶囊内全部文件（排除 `capsule.yaml` 与点开头路径）的
    相对路径 → sha256 前 16 位，供 `--end-task --evidence` 判「有没有产出」（G-7）。
    同一胶囊已有 open 的 Manager 段或旧段时拒绝；open 的 role 段不阻塞。
    """
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    current = _read_key(text, "lifecycle") or "draft"
    if current in TERMINAL:
        raise ToolError(f"❌ 胶囊已是终态 {current}，不能再开启任务段。")

    # IMP-M6：写前全局不变量校验，手改/损坏状态不得被新写入继承。
    _check_invariants(data)

    segments = [s for s in (data.get("task_segments") or []) if isinstance(s, dict)]
    # 方案 4.4 判定 7：Manager 段与旧段同胶囊只允许一个 open；role 段由调度自己结，
    # 不阻塞 Manager 开工（T7-a）。
    blocking = [s for s in segments
                if s.get("state") == "open" and s.get("actor") in (None, "manager")]
    if blocking:
        first = blocking[0]
        kind = "Manager 段" if first.get("actor") == "manager" else "旧段（无 actor）"
        raise ToolError(
            f"❌ 胶囊已有进行中的{kind}: {first.get('seg_id')} ({first.get('name')})\n"
            "👉 修复建议: 请先收尾进行中的段（--end-task --evidence <产出物>），再开启新任务段。"
        )

    cid = _capsule_id(capsule, text)
    allocated_seq, _ = _alloc_seq(data)
    seg_id = f"{cid}#{allocated_seq}"

    start_dt = normalize_to_cst(at) if at else dt.datetime.now(TZ_CST)
    start_iso = start_dt.isoformat()

    # 阶段缺省一律 misc（T3c 删 mtime 推断，方案 4.4 判定 3）：不按文件 mtime 猜阶段。
    if stage is None:
        stage = "misc"

    seg = {
        "seq": allocated_seq,
        "seg_id": seg_id,
        "name": name,
        "startAt": start_iso,
        "lastActivityAt": start_iso,
        "state": "open",
        "actor": "manager",
        "trigger": "manager",
        "baseline": _capsule_baseline(capsule),
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
        _check_invariants(data)
        _save_capsule_yaml(capsule, data, text)

    return {
        "capsule": str(capsule),
        "status": "started",
        "segment": seg,
        "dry_run": dry_run,
        "worklog_refs": []
    }


def _capsule_baseline(capsule: Path) -> dict[str, str]:
    """胶囊内文件指纹快照：{相对路径: sha256 前 16 位}（Tech-176 T7，方案 4.4 判定 7）。

    排除 `capsule.yaml`（自身是计量真源，不能拿自己当产出证据）与一切点开头路径
    （`.git`、`.capsule.yaml.lock` 等运行时产物）。只收文件，不收目录。
    """
    baseline: dict[str, str] = {}
    for path in sorted(capsule.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(capsule).as_posix()
        if rel == "capsule.yaml" or rel.startswith(".") or "/." in rel:
            continue
        try:
            baseline[rel] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except OSError:
            continue
    return baseline


@_locked  # T6：读→改→写全程持胶囊目录锁
def touch_task(capsule_dir: str | Path, at: str | None = None, dry_run: bool = False) -> dict:
    """刷新进行中 Manager 段（或旧段）的 lastActivityAt（Tech-176 T7）。

    open 的 role 段不在此列：它的时间戳只由调度写入（T9）。
    `--at` 是显式指定的时刻，优先于 `_save_capsule_yaml` 的自动刷新（后者取 now）：
    显式回填时刻若被 now 覆盖，`--touch --at` 就再也无法把事件时间设回过去。
    """
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    segments = [s for s in (data.get("task_segments") or []) if isinstance(s, dict)]
    open_seg = next((s for s in segments
                     if s.get("state") == "open" and s.get("actor") in (None, "manager")), None)
    if not open_seg:
        return {"capsule": str(capsule), "status": "no_open_segment", "dry_run": dry_run}

    # IMP-M6：写前全局不变量校验。
    _check_invariants(data)
    if not dry_run:
        _save_capsule_yaml(capsule, data, text, activity_at=at)
    return {"capsule": str(capsule), "status": "touched", "segment": open_seg, "dry_run": dry_run}


def _sha256_prefix(path: Path) -> str:
    """文件 sha256 前 16 位（与 baseline / evidence_sha256 同口径）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _manager_evidence_sha256(capsule: Path, evidence: str | None) -> str | None:
    """Manager 段证据指纹：证据须是胶囊内可见文件，且指纹与开段 baseline 不同。

    返回 16hex 指纹；缺 --evidence、路径在胶囊外、文件不存在或指纹未变返回 None
    （调用方据此转 unmeasured）。只判产出、不推时长（方案 4.4 判定 7 末行）。
    """
    if not evidence or not str(evidence).strip():
        return None
    rel = str(evidence).strip()
    target = (capsule / rel).resolve()
    if not target.is_file() or capsule not in target.parents:
        return None
    try:
        sha = _sha256_prefix(target)
    except OSError:
        return None
    return sha


def _manager_end_moment(seg: dict, now: dt.datetime) -> dt.datetime:
    """Manager 段结束时刻：尾空档 > IDLE_GAP_S 取最后一次工具事件，否则取 now（D-3）。"""
    last_raw = seg.get("lastActivityAt")
    last_dt = normalize_to_cst(last_raw) if last_raw else None
    if last_dt is not None and (now - last_dt).total_seconds() > IDLE_GAP_S:
        return last_dt
    return now


def _role_segment_overlap_seconds(segments: list[dict], start: dt.datetime,
                                  end: dt.datetime) -> int:
    """本胶囊所有 role 段与 [start, end] 的整数秒重叠（按并集计，不重复扣）。"""
    ranges: list[tuple[dt.datetime, dt.datetime]] = []
    for s in segments:
        if s.get("actor") != "role" or not s.get("startAt"):
            continue
        s_start = normalize_to_cst(s["startAt"])
        s_end = normalize_to_cst(s["endAt"]) if s.get("endAt") else end
        lo, hi = max(start, s_start), min(end, s_end)
        if hi > lo:
            ranges.append((lo, hi))
    if not ranges:
        return 0
    ranges.sort()
    total = 0.0
    cur_lo, cur_hi = ranges[0]
    for lo, hi in ranges[1:]:
        if lo > cur_hi:
            total += (cur_hi - cur_lo).total_seconds()
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    total += (cur_hi - cur_lo).total_seconds()
    return int(round(total))


@_locked  # T6：读→改→写全程持胶囊目录锁
def end_task(capsule_dir: str | Path, trigger: str = "deliver", at: str | None = None,
             inferred: bool = False, evidence: str | None = None, note: str | None = None,
             dry_run: bool = False) -> dict:
    """收尾任务段并结算工时。"""
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    current = _read_key(text, "lifecycle") or "draft"
    if current in TERMINAL:
        raise ToolError(f"❌ 胶囊已是终态 {current}，不能再收尾任务段。")

    _check_invariants(data)

    segments = [s for s in (data.get("task_segments") or []) if isinstance(s, dict)]
    # Manager 段优先取本胶囊 open 的 manager 段；没有则退回首个 open 段（旧段或 role 段，
    # 后者按 4.4.1 拒绝结算）。
    open_seg = next((s for s in segments if s.get("state") == "open" and s.get("actor") == "manager"),
                    None) or _find_open_segment(segments)
    if not open_seg:
        raise ToolError("❌ 胶囊当前没有进行中的 open 任务段，无需收尾。")

    # T8 段：actor 政策（方案 4.4.1）。Manager 段走证据门槛 + 尾空档 + 扣 role 段重叠；
    # role 段只由调度按 dispatch_id 结段，本入口拒绝结算（残留段用 --unmeasure-segment）。
    actor = open_seg.get("actor")
    if actor == "role":
        raise ToolError(
            f"❌ 段 {open_seg['seg_id']} 是角色调度段（actor=role），不能由 --end-task 结算\n"
            "👉 修复建议: 角色段由 dispatch_role.py run 自开自结；"
            f"残留段请用 --unmeasure-segment {open_seg['seg_id']} --reason <原因> 收尾。"
        )
    if actor == "manager":
        return _end_manager_task(capsule, data, text, open_seg, segments,
                                 trigger=trigger, evidence=evidence, note=note,
                                 at=at, dry_run=dry_run)

    # IMP-M2：使用显式 --at 回填或更正时必须提供 --evidence；
    # 若未传 --at 而仅以 Tool 记录的 lastActivityAt 中断回填，允许省略证据（恢复 AC-5(b)）。
    if inferred and at and not (evidence and str(evidence).strip()):
        raise ToolError(
            "❌ 显式 --at 覆盖推断时间时必须附 --evidence <说明>\n"
            "👉 修复建议: 补一条可核实的证据（如「会话中断于 10-15 18:00，提交 3f2a91c」）后重试。"
        )

    start_dt = normalize_to_cst(open_seg["startAt"])
    if inferred and not at:
        end_dt = normalize_to_cst(open_seg["lastActivityAt"])
    elif at:
        end_dt = normalize_to_cst(at)
    else:
        end_dt = dt.datetime.now(TZ_CST)

    duration_s = max(1, round((end_dt - start_dt).total_seconds()))
    if duration_s <= 0:
        raise ToolError(f"❌ 结束时间早于或等于开始时间: start={start_dt.isoformat()}, end={end_dt.isoformat()}")

    open_seg["endAt"] = end_dt.isoformat()
    open_seg["lastActivityAt"] = end_dt.isoformat()
    open_seg["trigger"] = trigger
    if inferred:
        open_seg["inferred"] = True
    if evidence:
        open_seg["evidence"] = str(evidence).strip()
    if note:
        open_seg["note"] = note

    new_worklog = _settle_segment(data, open_seg, duration_s)
    worklog_refs = [open_seg["seg_id"]] if new_worklog is not None else []

    if not dry_run:
        _check_invariants(data)
        _save_capsule_yaml(capsule, data, text)

    return {
        "capsule": str(capsule),
        "status": "ended",
        "segment": open_seg,
        "entry": new_worklog,
        "worklog_refs": worklog_refs,
        "dry_run": dry_run
    }


def _end_manager_task(capsule: Path, data: dict[str, Any], text: str, seg: dict,
                      segments: list[dict], *, trigger: str, evidence: str | None,
                      note: str | None, at: str | None, dry_run: bool) -> dict:
    """Manager 段结段（Tech-176 T8，方案 4.4 判定 7、4.4.2）。

    ① 不接受 --at / --inferred：结束时刻由尾空档规则决定；
    ② 证据门槛：--evidence 须指向胶囊内文件且指纹与开段 baseline 不同，否则段转
       unmeasured、退出码 0、不写 worklog；
    ③ 有效秒 = (尾空档截断后的区间) − 与同胶囊 role 段的重叠秒；≤0 → unmeasured
       （「全部与角色调度重叠」）；
    ④ 否则按 effective_seconds 走 ≤8h / 8–24h / >24h 门槛（4.4.2：结果 ≤0 不取最少 1 秒）。
    """
    if at or trigger not in ("deliver", "delivered"):
        # --at 会绕过尾空档与重叠扣除的口径；Manager 段结束时刻只由工具事件决定。
        raise ToolError(
            "❌ Manager 段不接受 --at（结束时刻由最后一次工具事件与尾空档阈值决定）\n"
            "👉 修复建议: 去掉 --at 重试；确需修正结束时间请先用 --unmeasure-segment 收尾再人工补登。"
        )

    seg_id = seg["seg_id"]
    evidence_rel = str(evidence).strip() if evidence and str(evidence).strip() else None
    sha = _manager_evidence_sha256(capsule, evidence_rel)
    baseline = seg.get("baseline") or {}
    unchanged = sha is not None and any(v == sha for v in baseline.values())

    def finish(state: str, *, reason: str, seconds: int | None = None) -> dict:
        seg["state"] = state
        seg["trigger"] = trigger
        seg["note"] = _append_note(seg.get("note"), reason)
        if evidence_rel:
            seg["evidence"] = evidence_rel
        if sha:
            seg["evidence_sha256"] = sha
        if not dry_run:
            _check_invariants(data)
            _save_capsule_yaml(capsule, data, text)
        return {"capsule": str(capsule), "status": "ended", "segment": seg, "entry": None,
                "worklog_refs": [], "dry_run": dry_run}

    if evidence_rel is None:
        return finish("unmeasured", reason="未带 --evidence <产出物>：无产出证据，段作废不入账")
    if sha is None:
        return finish("unmeasured", reason=f"证据文件无效（不在胶囊内或不存在）: {evidence_rel}")
    if unchanged:
        return finish("unmeasured", reason=f"证据文件指纹与开段 baseline 相同，无新产出: {evidence_rel}")

    now = dt.datetime.now(TZ_CST)
    end_dt = _manager_end_moment(seg, now)
    start_dt = normalize_to_cst(seg["startAt"])
    if end_dt <= start_dt:
        return finish("unmeasured", reason="区间非正：开段时间晚于最后一次工具事件")
    seg["endAt"] = end_dt.isoformat()
    seg["lastActivityAt"] = end_dt.isoformat()
    seg["evidence"] = evidence_rel
    seg["evidence_sha256"] = sha

    span_s = int(round((end_dt - start_dt).total_seconds()))
    overlap_s = _role_segment_overlap_seconds(segments, start_dt, end_dt)
    effective = span_s - overlap_s
    if effective <= 0:
        if span_s <= 0:
            # 区间本身不足 1 秒（如 100ms 舍入到 0）：与角色重叠无关，不误报
            return finish("unmeasured",
                          reason=f"区间不足 1 秒、不可计量（区间 {span_s}s）")
        return finish("unmeasured",
                      reason=f"全部与角色调度重叠（区间 {span_s}s，重叠 {overlap_s}s）")
    seg["machine_seconds"] = max(1, span_s)
    seg["effective_seconds"] = effective
    if note:
        seg["note"] = note
    entry = _settle_segment(data, seg, effective, machine_seconds=max(1, span_s))
    worklog_refs = [seg_id] if entry is not None else []
    if not dry_run:
        _check_invariants(data)
        _save_capsule_yaml(capsule, data, text)
    return {"capsule": str(capsule), "status": "ended", "segment": seg, "entry": entry,
            "worklog_refs": worklog_refs, "dry_run": dry_run}


@_locked  # T6：读→改→写全程持胶囊目录锁
def approve_segment(capsule_dir: str | Path, seg_id: str, dry_run: bool = False) -> dict:
    """人工批准超过 8h 但不超过 24h 的任务段入账。"""
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    current = _read_key(text, "lifecycle") or "draft"
    if current in TERMINAL:
        raise ToolError(f"❌ 胶囊已是终态 {current}，不能再批准任务段。")

    _check_invariants(data)

    segments = [s for s in (data.get("task_segments") or []) if isinstance(s, dict)]
    target_seg = next((s for s in segments if s.get("seg_id") == seg_id), None)
    if not target_seg:
        raise ToolError(f"❌ 未找到任务段: {seg_id}")
    # 方案 4.4.1：approve 读段上已有的 effective_seconds 入账（manager 段与 role 段都不得
    # 按墙钟重算）。已由调度按 dispatch_id 结为 pending_approval 的 role 段按其
    # effective_seconds 批准（方案 4.4.1:200）；open 的 role 段没有 dispatch_id 身份，
    # 仍拒绝（AC-15），残留段用 --unmeasure-segment。
    if target_seg.get("actor") == "role" and target_seg.get("state") != "pending_approval":
        raise _refuse_role_segment("--approve-segment", target_seg)
    if target_seg.get("state") != "pending_approval":
        raise ToolError(f"❌ 任务段 {seg_id} 状态为 {target_seg.get('state')}，不是 pending_approval，无需批准。")

    # 门槛按有效秒判定（方案 4.4.2：8h/24h 读同一个 effective_seconds）。actor 段读段上
    # effective_seconds；旧段（无 actor）没有该字段，沿用墙钟口径。墙钟只作展示。
    wall_s = _segment_duration_seconds(target_seg)
    raw_effective = target_seg.get("effective_seconds")
    effective = (max(0, round(raw_effective))
                 if isinstance(raw_effective, (int, float))
                 else wall_s)
    if effective > APPROVAL_LIMIT_S:
        raise ToolError(
            f"❌ 任务段 {seg_id} 有效时长 > 24h（{fmt_hms(effective)}），禁止直接批准；\n"
            f"   墙钟 {fmt_hms(wall_s)}（仅展示）。\n"
            "👉 修复建议: 用 --fix-segment 修正结束时间，或用 --split-segment --at <iso> 按时刻拆段。"
        )

    target_seg["state"] = "logged"
    target_seg["machine_seconds"] = max(1, wall_s)
    spent = _seconds_to_spent(effective)
    stage = target_seg.get("stage") or "misc"
    operator = _default_operator()
    work_date = normalize_to_cst(target_seg["startAt"]).strftime("%Y-%m-%d")

    entry: dict[str, Any] = {
        "date": work_date,
        "stage": stage,
        "spent": spent,
        "seconds": effective,
        "summary": f"{target_seg['name']}（人工批准入账）"[:120],
        "operator": operator,
        "ref": target_seg["seg_id"],
    }
    if target_seg.get("actor") in ("role", "manager"):
        entry["actor"] = target_seg["actor"]
        if target_seg.get("actor") == "role" and target_seg.get("role"):
            entry["role"] = target_seg["role"]
    worklogs = data.get("worklogs") or []
    worklogs.append(entry)
    data["worklogs"] = worklogs

    if not dry_run:
        _check_invariants(data)
        _save_capsule_yaml(capsule, data, text)

    return {
        "capsule": str(capsule),
        "status": "approved",
        "segment": target_seg,
        "entry": entry,
        "worklog_refs": [seg_id],
        "dry_run": dry_run
    }


@_locked  # T6：读→改→写全程持胶囊目录锁
def unmeasure_segment(capsule_dir: str | Path, seg_id: str, reason: str, dry_run: bool = False) -> dict:
    """显式将待批段标记为不计量。

    IMP-C3：只接受 open / pending_approval 源状态。logged 是终态且已有对应 worklog，
    撤段会破坏 I-3（logged 段与 worklogs.ref 一一对应）；已入账调整走 --override。
    """
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    current = _read_key(text, "lifecycle") or "draft"
    if current in TERMINAL:
        raise ToolError(f"❌ 胶囊已是终态 {current}，不能再修改任务段。")

    segments = [s for s in (data.get("task_segments") or []) if isinstance(s, dict)]
    target_seg = next((s for s in segments if s.get("seg_id") == seg_id), None)
    if not target_seg:
        raise ToolError(f"❌ 未找到任务段: {seg_id}")
    state = target_seg.get("state")
    if state not in UNMEASURABLE_SOURCES:
        raise ToolError(
            f"❌ 任务段 {seg_id} 状态为 {state}，不能标记为不计量"
            f"（只接受 {' / '.join(UNMEASURABLE_SOURCES)}）\n"
            "👉 修复建议: 已入账（logged）段请用 --override 调整工时，不改段状态；unmeasured 已是终态。"
        )
    target_seg["state"] = "unmeasured"
    target_seg["note"] = f"不计量: {reason}"
    if not dry_run:
        _check_invariants(data)
        _save_capsule_yaml(capsule, data, text)
    return {"capsule": str(capsule), "status": "unmeasured", "segment": target_seg, "dry_run": dry_run}


@_locked  # T6：读→改→写全程持胶囊目录锁
def fix_segment(capsule_dir: str | Path, seg_id: str, end_at: str, dry_run: bool = False) -> dict:
    """IMP-M1：修正 >24h 异常段的结束时间，随后按新时长重新结算。

    段必须处于 open / pending_approval；logged / unmeasured 是终态，改结束时间会与
    已落账事实冲突。
    """
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    current = _read_key(text, "lifecycle") or "draft"
    if current in TERMINAL:
        raise ToolError(f"❌ 胶囊已是终态 {current}，不能再修正任务段。")

    _check_invariants(data)

    segments = [s for s in (data.get("task_segments") or []) if isinstance(s, dict)]
    target_seg = next((s for s in segments if s.get("seg_id") == seg_id), None)
    if not target_seg:
        raise ToolError(f"❌ 未找到任务段: {seg_id}")
    # 方案 4.4.1：fix 只接受 pending_approval 的 Manager 段（改 endAt 后按 4.4 步骤②重算
    # effective_seconds，证据沿用段上已校验的 evidence_sha256）；open 段拒绝（用
    # --end-task --evidence）；role 段拒绝（时间戳只由调度写）。
    seg_actor = target_seg.get("actor")
    if seg_actor == "role":
        raise _refuse_role_segment("--fix-segment", target_seg)
    if seg_actor == "manager" and target_seg.get("state") != "pending_approval":
        raise ToolError(
            f"❌ Manager 段 {seg_id} 状态为 {target_seg.get('state')}，不能修正结束时间\n"
            "👉 修复建议: 只接受 pending_approval；open 的 Manager 段请用 --end-task --evidence <产出物> 收尾。"
        )
    if target_seg.get("state") not in ("open", "pending_approval"):
        raise ToolError(
            f"❌ 任务段 {seg_id} 状态为 {target_seg.get('state')}，不能修正结束时间\n"
            "👉 修复建议: 只接受 open / pending_approval；已入账段请用 --override 调整工时。"
        )

    end_dt = normalize_to_cst(end_at)
    start_dt = normalize_to_cst(target_seg["startAt"])
    if end_dt <= start_dt:
        raise ToolError(
            f"❌ 修正后的结束时间必须晚于开始时间: start={start_dt.isoformat()}, end={end_dt.isoformat()}\n"
            "👉 修复建议: 结束时间须落在段开始之后；要按时刻拆段请用 --split-segment。"
        )

    target_seg["endAt"] = end_dt.isoformat()
    target_seg["lastActivityAt"] = end_dt.isoformat()
    target_seg["note"] = (target_seg.get("note") or "").strip()
    if seg_actor == "manager":
        # 4.4「7 Manager 结段」步骤②：区间秒 = (end − startAt) − 与同胶囊 role 段并集重叠秒。
        span_s = max(1, round((end_dt - start_dt).total_seconds()))
        overlap_s = _role_segment_overlap_seconds(segments, start_dt, end_dt)
        effective = span_s - overlap_s
        if effective <= 0:
            raise ToolError(
                f"❌ 修正后的 Manager 段 {seg_id} 有效秒非正（区间 {span_s}s，重叠 {overlap_s}s）\n"
                "👉 修复建议: 换一个不与角色调度重叠的结束时间，或用 --unmeasure-segment 收尾。"
            )
        target_seg["effective_seconds"] = effective
        entry = _settle_segment(data, target_seg, effective, machine_seconds=span_s)
    else:
        entry = _settle_segment(data, target_seg, max(1, round((end_dt - start_dt).total_seconds())))

    if not dry_run:
        _check_invariants(data)
        _save_capsule_yaml(capsule, data, text)

    return {
        "capsule": str(capsule),
        "status": "fixed",
        "segment": target_seg,
        "entry": entry,
        "worklog_refs": [seg_id] if entry is not None else [],
        "dry_run": dry_run,
    }


@_locked  # T6：读→改→写全程持胶囊目录锁
def split_segment(capsule_dir: str | Path, seg_id: str, at: list[str], dry_run: bool = False) -> dict:
    """IMP-M1：把 >24h 异常段按给定时刻拆成连续子段。

    原段保留事实并转 unmeasured（note 记录去向），每个子段从同一 seq_next 分配新 seq，
    逐个按 §3.3 规则结算（≤8h 自动入账 / 8～24h 待批 / >24h 仍待拆）。
    """
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    current = _read_key(text, "lifecycle") or "draft"
    if current in TERMINAL:
        raise ToolError(f"❌ 胶囊已是终态 {current}，不能再拆分任务段。")

    _check_invariants(data)

    if not at:
        raise ToolError("❌ --split-segment 必须至少给一个 --at <ISO 时刻>")

    segments = [s for s in (data.get("task_segments") or []) if isinstance(s, dict)]
    target_seg = next((s for s in segments if s.get("seg_id") == seg_id), None)
    if not target_seg:
        raise ToolError(f"❌ 未找到任务段: {seg_id}")
    # 方案 4.4.1：actor 段禁止拆段——重叠扣除和证据不能按时刻切分（manager），子段还会
    # 复制 dispatch_id 违反 I-9（role）。>24h 用 --fix-segment 或 --unmeasure-segment。
    if target_seg.get("actor") in ("role", "manager"):
        raise ToolError(
            f"❌ --split-segment 不接受 actor={target_seg.get('actor')} 段: {seg_id}\n"
            "👉 修复建议: 重叠扣除与证据不能按时刻切分；超 24h 请用 --fix-segment 修正结束时间，"
            "或用 --unmeasure-segment 收尾后人工补登。"
        )
    if target_seg.get("state") not in ("open", "pending_approval"):
        raise ToolError(
            f"❌ 任务段 {seg_id} 状态为 {target_seg.get('state')}，不能拆分\n"
            "👉 修复建议: 只接受 open / pending_approval；已入账段请用 --override 调整工时。"
        )

    start_dt = normalize_to_cst(target_seg["startAt"])
    end_dt = normalize_to_cst(target_seg["endAt"]) if target_seg.get("endAt") else dt.datetime.now(TZ_CST)
    if end_dt <= start_dt:
        raise ToolError(f"❌ 任务段 {seg_id} 结束时间不晚于开始时间，无法拆分")

    cuts = sorted({normalize_to_cst(x) for x in at})
    if len({c.isoformat() for c in cuts}) != len(cuts):
        raise ToolError("❌ --at 时刻重复，请给互不相同的拆分点")
    bounds = [start_dt, *cuts, end_dt]
    for prev, nxt in zip(bounds, bounds[1:]):
        if nxt <= prev:
            raise ToolError(
                f"❌ 拆分时刻必须严格位于段内且递增: {prev.isoformat()} → {nxt.isoformat()}\n"
                "👉 修复建议: 所有 --at 须落在 startAt 与 endAt 之间。"
            )

    cid = _capsule_id(capsule, text)
    new_segments: list[dict] = []
    new_entries: list[dict] = []
    for i, (seg_start, seg_end) in enumerate(zip(bounds, bounds[1:]), start=1):
        allocated_seq, _ = _alloc_seq(data)
        part = {
            "seq": allocated_seq,
            "seg_id": f"{cid}#{allocated_seq}",
            "name": f"{target_seg['name']}（拆分 {i}/{len(bounds) - 1}）",
            "startAt": seg_start.isoformat(),
            "lastActivityAt": seg_end.isoformat(),
            "endAt": seg_end.isoformat(),
            "state": "open",
            "trigger": "switch",
            "note": f"由 {seg_id} 于 {seg_start.isoformat()} 拆分而来",
        }
        for key in ("taskId", "stage"):
            if target_seg.get(key):
                part[key] = target_seg[key]
        entry = _settle_segment(data, part, max(1, round((seg_end - seg_start).total_seconds())))
        errs = _validate_segment(part)
        if errs:
            raise ToolError(f"❌ 拆分段数据契约校验失败: {'；'.join(errs)}")
        new_segments.append(part)
        if entry is not None:
            new_entries.append(entry)

    # 原段保留事实并显式留痕去向（方案 §3.4：拆段后的原段为 unmeasured 终态）。
    target_seg["state"] = "unmeasured"
    target_seg["note"] = (f"不计量: 超过 24h 已拆分为 "
                          + ", ".join(s["seg_id"] for s in new_segments))

    data["task_segments"] = [*segments, *new_segments]

    if not dry_run:
        _check_invariants(data)
        _save_capsule_yaml(capsule, data, text)

    return {
        "capsule": str(capsule),
        "status": "split",
        "segment": target_seg,
        "segments": new_segments,
        "entries": new_entries,
        "worklog_refs": [e["ref"] for e in new_entries],
        "dry_run": dry_run,
    }


@_locked  # T6：读→改→写全程持胶囊目录锁
def override_worklog(capsule_dir: str | Path, ref: str, spent: float | str | None = None,
                     summary: str | None = None, stage: str | None = None, reason: str | None = None,
                     dry_run: bool = False) -> dict:
    """人工覆盖工时条目（G-8 / AC-12：可审计的纠错动作）。

    `--reason` 必填（D-5）：覆盖属于纠错，必须留原因。`--spent` 接受与 `--log-time`
    相同的时长语法（纯数字按小时、`1h23m`、`01:23:45`），换算成整数秒写 `seconds`
    （真源）并派生 `spent`。`--stage` 改阶段，首次改阶段时保存 `original_stage`。
    首次覆盖保存 `original_seconds`（无 seconds 的旧条目按 round(spent×3600) 补算）、
    `original_spent`、`original_summary`；每次覆盖写 `override_reason`（最近一次原因），
    二次覆盖不改 `original_*`。
    """
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    current = _read_key(text, "lifecycle") or "draft"
    if current in TERMINAL:
        raise ToolError(f"❌ 胶囊已是终态 {current}，不能再覆盖工时。")

    _check_invariants(data)

    reason_clean = str(reason).strip() if reason is not None else ""
    if not reason_clean:
        raise ToolError(
            "❌ --override 必须提供 --reason <原因>（覆盖是纠错动作，须可审计）\n"
            "👉 修复建议: 补一条 200 字以内的原因，如 --reason \"P-1 mtime 推断错位\"。"
        )
    if len(reason_clean) > 200:
        raise ToolError("❌ 覆盖原因不得超过 200 字。")

    worklogs = [w for w in (data.get("worklogs") or []) if isinstance(w, dict)]
    target_log = next((w for w in worklogs if w.get("ref") == ref), None)
    if not target_log:
        raise ToolError(f"❌ 未找到 ref 为 {ref} 的工时条目。")

    if spent is None and summary is None and stage is None:
        raise ToolError("❌ 必须提供 --spent / --summary / --stage 之一进行覆盖。")

    # 首次覆盖保存 original_*（二次覆盖只更新当前值与 override_reason）。
    if not target_log.get("overridden"):
        target_log["overridden"] = True
        target_log["original_spent"] = target_log["spent"]
        target_log["original_summary"] = target_log.get("summary", "")
        if target_log.get("seconds") is not None:
            target_log["original_seconds"] = int(target_log["seconds"])
        else:
            target_log["original_seconds"] = max(1, round(float(target_log["spent"]) * SECONDS_PER_HOUR))

    if spent is not None:
        seconds = parse_duration(spent)
        target_log["seconds"] = seconds
        target_log["spent"] = _seconds_to_spent(seconds)
    if summary is not None:
        summary_clean = str(summary).strip()
        if not summary_clean or len(summary_clean) > 120:
            raise ToolError("❌ 摘要不能为空且不得超过 120 字。")
        target_log["summary"] = summary_clean
    if stage is not None:
        stage_clean = str(stage).strip()
        if not WORKLOG_STAGE_RE.match(stage_clean):
            raise ToolError(f"❌ stage 取值非法: {stage_clean!r}\n👉 修复建议: 形如 02_方案 或 misc。")
        if "original_stage" not in target_log:
            target_log["original_stage"] = target_log.get("stage")
        target_log["stage"] = stage_clean

    target_log["override_reason"] = reason_clean

    errs = _validate_entry(target_log)
    if errs:
        raise ToolError(f"❌ 覆盖后工时条目不合法: {'；'.join(errs)}")

    # IMP-M2：覆盖是活动事件，同步刷新当前 open 段的 lastActivityAt。
    _touch_open_segment(data)

    data["worklogs"] = worklogs
    if not dry_run:
        _check_invariants(data)
        _save_capsule_yaml(capsule, data, text)

    return {
        "capsule": str(capsule),
        "status": "overridden",
        "entry": target_log,
        "worklog_refs": [ref],
        "dry_run": dry_run,
        "total": worklog_total(capsule)
    }

def _sync_stage_deliverable_effort(capsule: Path, stage: str, seconds: int) -> None:
    """把工时登记的秒数同步注入对应阶段产物的 Front Matter 中（自包含元信息）。

    Tech-176 T13（方案 4.4 判定「1 effort」）：`effort` 写 H:MM:SS（如 `0:07:00`），
    旧 `0.75h` 写法仍合法（front_matter pattern 双向兼容）。阶段映射键与 ROLE_STAGE
    的角色段阶段对齐：补 `03_方案审计`、`05_实施审计`、`08_汇报`。
    """
    candidates_map = {
        "01_调研": ["01_调研.md"],
        "02_方案": ["02_方案.md"],
        "03_设计": ["03_设计.md"],
        "04_实施": ["04_Spec.md", "04_实施说明.md"],
        "05_审修": ["05_实施审计报告.md", "03_方案审计报告.md"],
        "05_审计": ["05_实施审计报告.md", "03_方案审计报告.md"],
        "05_实施审计": ["05_实施审计报告.md"],
        "03_方案审计": ["03_方案审计报告.md"],
        "07_验收": ["07_验收报告.md"],
        "08_汇报": ["08_汇报.md"],
    }
    file_names = candidates_map.get(stage, [])
    target: Path | None = None
    for name in file_names:
        cand = capsule / name
        if cand.is_file():
            target = cand
            break
    if target is None and stage == "04_实施":
        specs = sorted(capsule.glob("04_Spec_*.md"))
        if specs:
            target = specs[0]

    if target is not None and target.is_file():
        try:
            text = target.read_text(encoding="utf-8")
            m = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
            if m:
                block = m.group(1)
                line = f"effort: {fmt_hms(seconds)}"
                if re.search(r"^effort:.*$", block, re.MULTILINE):
                    block = re.sub(r"^effort:.*$", line, block, count=1, flags=re.MULTILINE)
                else:
                    block = block.rstrip() + "\n" + line
                target.write_text(f"---\n{block}\n---\n{text[m.end():]}", encoding="utf-8")
        except OSError:
            pass

@_locked  # T6：读→改→写全程持胶囊目录锁
def log_time(capsule_dir: str | Path, spent: float | str | None = None, seconds: int | None = None,
             stage: str | None = None,
             summary: str | None = None,
             operator: str | None = None, day: str | None = None, dry_run: bool = False,
             idempotent: bool = False, actor: str | None = None) -> dict:
    """胶囊 worklogs 的唯一人工补登写入入口。

    时长两种给法：`spent` 走 `parse_duration` 的三种写法（纯数字按小时、`1h23m`、
    `01:23:45`）；`seconds` 直接给整数秒（Tech-176 T13 下游入口 `--time` 换算后传入）。
    两者都换算成整数秒写 `seconds`（真源）与派生的 `spent`；actor 固定 `human`
    （方案 4.4 判定 2：人工只有这一条入口）。缺省 stage 一律 `misc`，不推断。
    """
    if seconds is None:
        if spent is None:
            raise ToolError("❌ 登记时长缺失\n👉 修复建议: 传 spent（1.5 / 1h23m / 01:23:45）或 seconds（整数秒）。")
        seconds = parse_duration(spent)
    else:
        seconds = int(seconds)
        if seconds < 1 or seconds > MAX_WORKLOG_SECONDS:
            raise ToolError(
                f"❌ 时长超出范围: {seconds}s（须 1..{MAX_WORKLOG_SECONDS}s，即 >0 且 ≤24h）"
            )
    capsule = Path(capsule_dir).expanduser().resolve()
    data, text = _load_capsule_data(capsule)
    current = _read_key(text, "lifecycle") or "draft"
    if current in TERMINAL:
        raise ToolError(
            f"❌ 胶囊已是终态 {current}，不能再登记工时\n"
            f"👉 修复建议: 工时只记进行中的事务；如需补记请先把 lifecycle 改回 active。"
        )

    _check_invariants(data)

    if actor is not None and str(actor).strip() != "human":
        raise ToolError(
            f"❌ --actor 只接受 human: {actor!r}\n"
            "👉 修复建议: 补登是唯一的人工入口，固定 actor=human；角色与 Manager 工时由段自动计量。"
        )

    stage_from = "arg" if stage is not None else "default"
    stage = str(stage).strip() if stage is not None else "misc"
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

    hours = _seconds_to_spent(seconds)

    # 分配持久 ref
    cid = _capsule_id(capsule, text)
    allocated_seq, _ = _alloc_seq(data)
    ref = f"{cid}#{allocated_seq}"

    entry = {
        "date": day,
        "stage": stage,
        "spent": hours,
        "seconds": seconds,
        "actor": "human",
        "summary": summary,
        "operator": operator,
        "ref": ref
    }
    errs = _validate_entry(entry)
    if errs:
        raise ToolError(f"❌ 工时条目不符合 schemas/capsule.schema.json: {'；'.join(errs)}")

    existing = [w for w in (data.get("worklogs") or []) if isinstance(w, dict)]
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
    _touch_open_segment(data)

    if not dry_run:
        _check_invariants(data)
        _save_capsule_yaml(capsule, data, text)
        if stage:
            _sync_stage_deliverable_effort(capsule, stage, seconds)
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


ROLE_STAGE: dict[str, str] = {
    "Researcher": "01_调研",
    "Architecture": "02_方案",
    "Designer": "03_设计",
    "Builder": "04_实施",
    "Reviewer": "05_实施审计",
    "Maintainer": "07_验收",
    "Reporter": "08_汇报",
}


def _dispatch_stage(role: str, deliverable: str | None) -> str:
    """角色段阶段：按 ROLE_STAGE 映射；Reviewer 按交付物文件名前缀分方案/实施审计（D-9）。"""
    if role == "Reviewer":
        name = Path(deliverable).name if deliverable else ""
        return "03_方案审计" if name.startswith("03_") else "05_实施审计"
    return ROLE_STAGE.get(role, "misc")


def _dispatch_segment_name(role: str, deliverable: str | None) -> str:
    """段名：`<角色>→<交付物文件名或 verify>`（方案 4.4 判定 3）。"""
    target = Path(deliverable).name if deliverable else "verify"
    return f"{role}→{target}"


def _find_capsule_for_dispatch(capsule: str | Path | None,
                               path: str | Path | None) -> Path | None:
    """定位目标胶囊：capsule 显式优先，其次 path 向上就近；找不到返回 None。"""
    if capsule is not None:
        cand = Path(capsule).expanduser().resolve()
        return cand if (cand / "capsule.yaml").is_file() else None
    if path is not None:
        p = Path(path).resolve()
        return next((d for d in [p.parent, *p.parents] if (d / "capsule.yaml").is_file()), None)
    return None


def start_dispatch_segment(capsule: str | Path | None = None, path: str | Path | None = None,
                           dispatch_id: str = "", role: str = "",
                           deliverable: str | Path | None = None,
                           stage: str | None = None) -> dict | None:
    """开角色调度段（Tech-176 T9，方案 4.4 判定 3、4.2 第 4 步）。

    写 `actor: role` / `role` / `dispatch_id` / `trigger: dispatch`，阶段按
    `ROLE_STAGE` 映射（Reviewer 按交付物前缀分方案/实施审计，D-9）。沿用
    `end_open_task_for` 的静默契约：无胶囊、胶囊终态、数据异常或锁超时一律返回
    None——工时缺失不得反向阻断调度（方案 9.4）。open 的 Manager 段或旧段不阻塞：
    Manager 段与角色段是不同 actor 的并行事实。
    """
    if not dispatch_id or not role:
        return None
    cdir = _find_capsule_for_dispatch(capsule, path)
    if cdir is None:
        return None
    try:
        with _capsule_lock(cdir):
            return _start_dispatch_segment_locked(cdir, dispatch_id, role, deliverable, stage)
    except (ToolError, OSError):
        return None


def _start_dispatch_segment_locked(cdir: Path, dispatch_id: str, role: str,
                                   deliverable: str | Path | None,
                                   stage: str | None) -> dict | None:
    """`start_dispatch_segment` 的持锁实现：读→改→写全程在 `.capsule.yaml.lock` 内。"""
    data, text = _load_capsule_data(cdir)
    if (_read_key(text, "lifecycle") or "draft") in TERMINAL:
        return None
    _check_invariants(data)
    segments = [s for s in (data.get("task_segments") or []) if isinstance(s, dict)]
    if any(s.get("state") == "open" and s.get("dispatch_id") == dispatch_id for s in segments):
        return None  # 同 dispatch_id 已有 open 段，不重复开段
    allocated_seq, _ = _alloc_seq(data)
    cid = _capsule_id(cdir, text)
    now = dt.datetime.now(TZ_CST).isoformat()
    seg = {
        "seq": allocated_seq,
        "seg_id": f"{cid}#{allocated_seq}",
        "name": _dispatch_segment_name(role, str(deliverable) if deliverable else None),
        "startAt": now,
        "lastActivityAt": now,
        "state": "open",
        "actor": "role",
        "role": role,
        "dispatch_id": dispatch_id,
        "trigger": "dispatch",
        "stage": stage or _dispatch_stage(role, str(deliverable) if deliverable else None),
    }
    errs = _validate_segment(seg)
    if errs:
        return None
    segments.append(seg)
    data["task_segments"] = segments
    _check_invariants(data)
    _save_capsule_yaml(cdir, data, text)
    return {"capsule": str(cdir), "status": "started", "segment": seg,
            "dry_run": False, "worklog_refs": []}


def end_dispatch_segment(dispatch_id: str, succeeded: bool,
                         failed_attempt_seconds: int = 0,
                         failure_code: str = "",
                         capsule: str | Path | None = None,
                         path: str | Path | None = None,
                         deliverable: str | Path | None = None) -> dict | None:
    """按 dispatch_id 结角色调度段（Tech-176 T9，方案 4.4 判定 3/4、4.2 第 7 步）。

    只结本胶囊内 `dispatch_id` 相同的 open 段（P-2：并发不错结别人的段）。
    `succeeded=False` → `state: failed`，写 `machine_seconds` 与 `note: <failure_code>`，
    不生成 worklog（AC-7：计机时不计有效工时）。成功时 `effective_seconds =
    machine_seconds − failed_attempt_seconds`（4.4.2，最少 1 秒），worklog
    `seconds` 等于它。静默契约同 `start_dispatch_segment`。
    """
    if not dispatch_id:
        return None
    cdir = _find_capsule_for_dispatch(capsule, path)
    if cdir is None:
        return None
    try:
        with _capsule_lock(cdir):
            return _end_dispatch_segment_locked(cdir, dispatch_id, succeeded,
                                                failed_attempt_seconds, failure_code)
    except (ToolError, OSError):
        return None


def _end_dispatch_segment_locked(cdir: Path, dispatch_id: str, succeeded: bool,
                                 failed_attempt_seconds: int, failure_code: str) -> dict | None:
    """`end_dispatch_segment` 的持锁实现：读→改→写全程在 `.capsule.yaml.lock` 内。"""
    data, text = _load_capsule_data(cdir)
    segments = [s for s in (data.get("task_segments") or []) if isinstance(s, dict)]
    seg = next((s for s in segments if s.get("state") == "open"
                and s.get("dispatch_id") == dispatch_id), None)
    if seg is None:
        return None
    _check_invariants(data)
    now = dt.datetime.now(TZ_CST)
    seg["endAt"] = now.isoformat()
    seg["lastActivityAt"] = now.isoformat()
    seg["trigger"] = "dispatch"
    machine = _segment_duration_seconds(seg, default_end=now)
    if not succeeded:
        seg["machine_seconds"] = machine
        seg["state"] = "failed"
        seg["note"] = _append_note(seg.get("note"), f"调度失败: {failure_code or 'unknown'}")
        _check_invariants(data)
        _save_capsule_yaml(cdir, data, text)
        return {"capsule": str(cdir), "status": "failed", "segment": seg,
                "entry": None, "worklog_refs": [], "dry_run": False}
    effective = max(1, machine - max(0, int(failed_attempt_seconds)))
    seg["machine_seconds"] = machine
    seg["effective_seconds"] = effective
    entry = _settle_segment(data, seg, effective, machine_seconds=machine)
    _check_invariants(data)
    _save_capsule_yaml(cdir, data, text)
    return {"capsule": str(cdir), "status": "ended", "segment": seg, "entry": entry,
            "worklog_refs": [seg["seg_id"]] if entry is not None else [], "dry_run": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="胶囊生命周期推进与工时计量：更新 capsule.yaml 的 lifecycle、任务段与 worklogs")
    parser.add_argument("capsule", help="胶囊目录（含 capsule.yaml）")
    parser.add_argument("--delivered", nargs="?", const=date.today().isoformat(), metavar="YYYY-MM-DD",
                        help="标记已交付（代码合入主干且全量测试通过），省略日期取今天")
    parser.add_argument("--log-time", metavar="DURATION",
                        help="登记工时（1.5 小时 / 1h23m / 01:23:45；固定 actor=human）")
    parser.add_argument("--actor", help="执行主体，只接受 human（补登唯一入口）")
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
    parser.add_argument("--fix-segment", metavar="SEG_ID", help="修正任务段结束时间（须配 --end-at）")
    parser.add_argument("--end-at", metavar="ISO", help="--fix-segment 使用的修正结束时间")
    parser.add_argument("--split-segment", metavar="SEG_ID", help="按 --at 时刻拆分任务段（可多次）")
    parser.add_argument("--override", metavar="REF", help="人工覆盖指定 ref 的工时条目")
    parser.add_argument("--spent", metavar="DURATION", help="覆盖后的工时（1.5 / 1h23m / 01:23:45）")

    parser.add_argument("--dry-run", action="store_true", help="只输出将要做的变更，不写入")
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
        elif args.fix_segment:
            if not args.end_at:
                raise ToolError("❌ --fix-segment 必须指定 --end-at <ISO 时刻>")
            res = fix_segment(args.capsule, seg_id=args.fix_segment, end_at=args.end_at, dry_run=args.dry_run)
        elif args.split_segment:
            if not args.at:
                raise ToolError("❌ --split-segment 必须指定至少一个 --at <ISO 时刻>")
            res = split_segment(args.capsule, seg_id=args.split_segment, at=[args.at], dry_run=args.dry_run)
        elif args.override:
            if not args.reason:
                raise ToolError("❌ --override 必须指定 --reason <原因>（覆盖是纠错动作，须可审计）")
            res = override_worklog(args.capsule, ref=args.override, spent=args.spent, summary=args.summary,
                                   stage=args.stage, reason=args.reason, dry_run=args.dry_run)
        elif args.log_time is not None:
            res = log_time(args.capsule, spent=args.log_time, stage=args.stage, summary=args.summary,
                           operator=args.operator, day=args.date, dry_run=args.dry_run,
                           idempotent=args.idempotent, actor=args.actor)
        else:
            res = update_capsule(args.capsule, delivered=args.delivered, dry_run=args.dry_run)
    except ToolError as err:
        print(str(err), file=sys.stderr)
        return 1

    prefix = "[dry-run] " if args.dry_run else ""
    status = res.get("status")
    if status == "started":
        print(f"{prefix}已开启任务段: {res['segment']['seg_id']} ({res['segment']['name']})")
    elif status == "ended":
        seg = res["segment"]
        ent = res.get("entry")
        if ent:
            print(f"{prefix}任务段收尾并入账: {seg['seg_id']} [{seg['state']}] {fmt_hms(ent.get('seconds', 0))} | ref: {ent.get('ref')}")
        elif seg.get("state") == "unmeasured":
            print(f"{prefix}⚠️ 任务段作废，未入账: {seg['seg_id']} ({seg.get('note') or '无证据'})")
        else:
            print(f"{prefix}任务段收尾: {seg['seg_id']} [{seg['state']}] ({seg.get('note') or '待审批'})")
    elif status == "approved":
        print(f"{prefix}任务段批准入账: {res['segment']['seg_id']} -> {fmt_hms(res['entry'].get('seconds', 0))}")
    elif status == "fixed":
        seg = res["segment"]
        ent = res.get("entry")
        if ent:
            print(f"{prefix}任务段时间已修正并入账: {seg['seg_id']} [{seg['state']}] {fmt_hms(ent.get('seconds', 0))}")
        else:
            print(f"{prefix}任务段时间已修正: {seg['seg_id']} [{seg['state']}] ({seg.get('note') or '待审批'})")
    elif status == "split":
        parts = ", ".join(f"{s['seg_id']}[{s['state']}]" for s in res["segments"])
        print(f"{prefix}任务段已拆分: {res['segment']['seg_id']} -> {parts}")
    elif status == "overridden":
        print(f"{prefix}已覆盖工时: {res['entry']['ref']} -> {fmt_hms(res['entry'].get('seconds', 0))}")
    elif status == "unmeasured":
        print(f"{prefix}任务段已标记为不计量: {res['segment']['seg_id']} ({res['segment'].get('note') or res.get('reason')})")
    elif status == "touched":
        print(f"{prefix}已刷新任务段活跃时间: {res.get('segment', {}).get('seg_id')}")
    elif status in ("added", "existing"):
        ent = res["entry"]
        print(f"{prefix}已登记工时: {ent['date']} [{ent['stage']}] {fmt_hms(ent.get('seconds', 0))} - {ent['summary']} ({ent['operator']}) | 累计: {fmt_hms(round(res['total'] * 3600))}")
    else:
        changes = res.get("changes", {})
        if changes:
            print(f"{prefix}lifecycle {res['from']} → {res['to']} " + " ".join(f"{k}={v}" for k, v in changes.items() if k != "lifecycle"))
        else:
            print(f"lifecycle={res['from']} 无需变更")
    return 0


if __name__ == "__main__":
    sys.exit(main())
