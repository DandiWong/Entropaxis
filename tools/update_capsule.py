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
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ORDER = ("draft", "active", "delivered", "archived", "archived-unmeasured")
TERMINAL = ("archived", "archived-unmeasured")
REVIEW_DAYS, CLOSURE_DAYS = 30, 60
BLOCKING = ("Critical", "Major")

# 工时登记契约（schemas/capsule.schema.json 的 worklogs 条目）。
# 步长、上限、摘要长度只在本文件与 schema 各声明一次；运行时校验统一走
# validate_schema（见 _validate_entry），避免两处实现漂移。
WORKLOG_STAGE_RE = re.compile(r"^(\d{2}_\S+|misc)$")
WORKLOG_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WORKLOG_ITEM_RE = re.compile(r"^\s*-\s")
WORKLOG_DEFAULT_INDENT = "  "


class ToolError(Exception):
    """可恢复业务异常，包含行动导向修复指引。"""


def _yaml_str(value: str) -> str:
    """自由文本 → JSON 双引号标量（JSON 字符串语法是 YAML 双引号标量的严格子集）。

    worklogs 每行都是流式映射，date/summary/operator 都经此序列化：未加引号的
    `2026-10-12` 会被 PyYAML 解析成 datetime.date，直接违反 capsule.schema.json 的
    `type: string`（Tech-120 审计 E-2 实测）。
    """
    return json.dumps(value, ensure_ascii=False)


def _fmt_hours(value: float) -> str:
    """1.5 → '1.5'，2.0 → '2'：Trailer 与提示语都用同一格式，不出现 2.0h 这种噪声。"""
    return f"{value:g}"


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


def _default_operator() -> str:
    """登记人缺省取 git config user.name，再退环境变量；都取不到即报错，不写空值。"""
    proc = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
    name = proc.stdout.strip() if proc.returncode == 0 else ""
    return name or os.environ.get("USER") or os.environ.get("USERNAME") or ""


def _default_stage(capsule: Path) -> tuple[str, str]:
    """--stage 缺省时取胶囊内 mtime 最新的 NN_*.md 阶段名。

    mtime 只用来猜阶段标签并回显给人复核，绝不用于推导工时时长（NG-1）。
    """
    stages = [p for p in capsule.glob("*.md") if re.match(r"^\d{2}_", p.name)]
    if not stages:
        return "misc", "default"
    newest = max(stages, key=lambda p: p.stat().st_mtime)
    return newest.name[:-3], "mtime"


def _worklog_block(text: str) -> tuple[int, int, str] | None:
    """定位 worklogs 块：返回 (键行下标, 块结束行下标, 首项缩进)。

    块结束行是第一个不属于该块的后续行（空行与注释不计入边界判断）。
    """
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if re.match(r"^worklogs:", ln)), None)
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if not ln.startswith((" ", "\t")):
            end = i
            break
    indent = WORKLOG_DEFAULT_INDENT
    for ln in lines[start + 1:end]:
        if WORKLOG_ITEM_RE.match(ln):
            indent = ln[: len(ln) - len(ln.lstrip())]
            break
    return start, end, indent


def read_worklogs(capsule_dir: str | Path) -> list[dict]:
    """宽容读取胶囊内 worklogs 条目（纯标准库，不依赖 PyYAML）。

    同时认流式映射（`- {date: "…", spent: 1.5}`，本工具唯一写法）与块式映射
    （人工手写）。字段值经 _yaml_str 反引号化，保证 date 读出即 str。
    """
    manifest = Path(capsule_dir) / "capsule.yaml"
    if not manifest.is_file():
        return []
    block = _worklog_block(manifest.read_text(encoding="utf-8", errors="replace"))
    if block is None:
        return []
    start, end, _ = block
    lines = manifest.read_text(encoding="utf-8", errors="replace").splitlines()
    items: list[dict] = []
    current: list[str] = []
    for ln in lines[start + 1:end]:
        if WORKLOG_ITEM_RE.match(ln):
            if current:
                items.append(_parse_worklog_item(current))
            current = [ln]
        elif current:
            current.append(ln)
    if current:
        items.append(_parse_worklog_item(current))
    return [i for i in items if i]


def _parse_worklog_item(lines: list[str]) -> dict:
    """把一个列表项的若干行拼成键值表；缺字段的项原样返回（由消费方判违约）。"""
    text = " ".join(ln.strip() for ln in lines)
    text = re.sub(r"^-\s*\{?", "", text).rstrip("}").strip()
    out: dict = {}
    for key in ("date", "stage", "spent", "summary", "operator"):
        m = re.search(rf"\b{key}:[ \t]*(\"[^\"]*\"|[^,}}]+)", text)
        if not m:
            continue
        raw = m.group(1).strip()
        if raw.startswith('"'):
            try:
                out[key] = json.loads(raw)
            except json.JSONDecodeError:
                out[key] = raw.strip('"')
        else:
            out[key] = raw.split("#", 1)[0].strip()
    if "spent" in out:
        try:
            out["spent"] = float(out["spent"])
        except (ValueError, TypeError):
            pass
    return out

def worklog_total(capsule_dir: str | Path) -> float:
    """胶囊内全部 worklogs 的工时和（Tasks 累计标记的真源，绝不盲加）。"""
    total = 0.0
    for item in read_worklogs(capsule_dir):
        try:
            total += float(item.get("spent"))
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
    try:  # 延迟导入：update_audit_state 反向导入本模块
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


def log_time(capsule_dir: str | Path, spent: float, stage: str | None = None, summary: str | None = None,
             operator: str | None = None, day: str | None = None, dry_run: bool = False,
             idempotent: bool = False) -> dict:
    """胶囊 worklogs 的唯一写入入口（Tech-120 G-2 单一真源）。

    写入形如 `  - {date: "2026-10-12", stage: "02_方案", spent: 1.5, summary: "…", operator: "…"}`
    的一行流式映射：纯标准库追加，行级 diff 可读，且所有字段值经 _yaml_str 带引号写入，
    PyYAML 解析回来仍是 str。

    参数契约：spent > 0、≤ 24、0.25 的整数倍；summary 非空（≤120 字符）；date 为
    YYYY-MM-DD；stage 形如 NN_xxx 或 misc；operator 缺省取 git user.name。

    重入语义（M-3）：
      - idempotent=False（默认命令行）：五元组 (date, stage, spent, summary, operator)
        完全相同时拒绝写入并提示，防止误重复登记；
      - idempotent=True（record_iteration / finish_branch 联动）：同样已存在时
        返回 {"status": "existing"} 并放行，让多文件写入的部分失败可原命令重入收敛。
    """
    capsule = Path(capsule_dir).expanduser().resolve()
    manifest = capsule / "capsule.yaml"
    if not manifest.is_file():
        raise ToolError(f"❌ 未找到胶囊清单: {manifest}\n"
                        "👉 修复建议: 传入含 capsule.yaml 的胶囊目录（可用 find_capsule.py 定位）。")
    text = manifest.read_text(encoding="utf-8")
    current = _read_key(text, "lifecycle") or "draft"
    if current in TERMINAL:
        raise ToolError(
            f"❌ 胶囊已是终态 {current}，不能再登记工时\n"
            f"👉 修复建议: 工时只记进行中的事务；如需补记请先把 lifecycle 改回 active（update_capsule.py 只管推进，回退需人工）。"
        )

    stage_from = "arg"
    if stage is None:
        stage, stage_from = _default_stage(capsule)
    stage = str(stage).strip()
    if not WORKLOG_STAGE_RE.match(stage):
        raise ToolError(
            f"❌ stage 取值非法: {stage!r}\n"
            "👉 修复建议: 形如 02_方案（胶囊内阶段名）或 misc；用 --stage 显式指定。"
        )
    if summary is None or not str(summary).strip():
        if sys.stdin is None or not sys.stdin.isatty():
            raise ToolError(
                "❌ 缺少工时摘要（--summary）\n"
                "👉 修复建议: 非交互式环境必须显式给 --summary \"这段做了什么\"；摘要让人能事后复核工时内容。"
            )
        summary = input("工时摘要（这段做了什么）: ").strip()
    summary = str(summary).strip()
    if not summary:
        raise ToolError("❌ 工时摘要不能为空\n👉 修复建议: 用 --summary \"这段做了什么\" 补一句可复核的描述。")
    operator = str(operator).strip() if operator else _default_operator()
    if not operator:
        raise ToolError(
            "❌ 无法确定登记人\n"
            "👉 修复建议: 先 `git config user.name <姓名>`，或用 --operator 显式指定。"
        )
    day = (str(day).strip() if day else date.today().isoformat())
    if not WORKLOG_DATE_RE.match(day):
        raise ToolError(f"❌ 日期格式错误: {day}\n👉 修复建议: 使用 YYYY-MM-DD。")
    try:
        hours = float(spent)
    except (TypeError, ValueError):
        raise ToolError(f"❌ 工时不合法: {spent!r}\n👉 修复建议: 传数字（如 1.5），步长 0.25h。") from None

    entry = {"date": day, "stage": stage, "spent": hours, "summary": summary, "operator": operator}
    errs = _validate_entry(entry)
    if errs:
        raise ToolError(
            f"❌ 工时条目不符合 schemas/capsule.schema.json: {'；'.join(errs)}\n"
            "👉 修复建议: spent 须 > 0、≤ 24 且为 0.25 的整数倍；summary ≤ 120 字；date 为 YYYY-MM-DD。"
        )

    existing = read_worklogs(capsule)
    for item in existing:
        match = (
            item.get("date") == entry["date"]
            and item.get("stage") == entry["stage"]
            and abs(float(item.get("spent", 0)) - float(entry["spent"])) < 1e-9
            and item.get("summary") == entry["summary"]
            and item.get("operator") == entry["operator"]
        )
        if match:
            if not idempotent:
                raise ToolError(
                    "❌ 该工时已登记过（date/stage/spent/summary/operator 五元组完全相同）\n"
                    "👉 修复建议: 确认不是误重复；确需放行（联动调用重入）加 --idempotent。"
                )
            return {"capsule": str(capsule), "status": "existing", "entry": entry,
                    "stage_from": stage_from, "dry_run": dry_run, "total": worklog_total(capsule)}
    line = (f"{WORKLOG_DEFAULT_INDENT}- {{date: {_yaml_str(entry['date'])}, stage: {_yaml_str(entry['stage'])}, "
            f"spent: {_fmt_hours(entry['spent'])}, summary: {_yaml_str(entry['summary'])}, "
            f"operator: {_yaml_str(entry['operator'])}}}")
    block = _worklog_block(text)
    if block is None:
        # 旧胶囊没有 worklogs 键：在末尾追加（含注释指向 schema）。
        new = (text.rstrip("\n")
               + "\n# 工时履历（唯一真源；契约 schemas/capsule.schema.json，写入只经 update_capsule.py log_time）\n"
               + "worklogs:\n" + line + "\n")
    else:
        start, end, _ = block
        lines = text.splitlines()
        if re.match(r"^worklogs:\s*\[\s*\]", lines[start]):
            lines[start] = "worklogs:"
            lines.insert(start + 1, line)
        else:
            lines.insert(end, line)
        new = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    result = {"capsule": str(capsule), "status": "added", "entry": entry,
              "stage_from": stage_from, "dry_run": dry_run,
              "total": worklog_total(capsule) + entry["spent"]}
    if dry_run:
        return result
    fd, tmp = tempfile.mkstemp(dir=capsule, prefix=".capsule-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
        os.replace(tmp, manifest)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    return result


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
    parser = argparse.ArgumentParser(description="胶囊生命周期推进与工时登记：更新 capsule.yaml 的 lifecycle、交付日期与 worklogs")
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
    parser.add_argument("--dry-run", action="store_true", help="只输出将要做的变更，不写入")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()
    try:
        res_lifecycle = None
        if args.delivered is not None or args.log_time is None:
            res_lifecycle = update_capsule(args.capsule, delivered=args.delivered, dry_run=args.dry_run)
        res_worklog = None
        if args.log_time is not None:
            res_worklog = log_time(args.capsule, spent=args.log_time, stage=args.stage, summary=args.summary,
                                   operator=args.operator, day=args.date, dry_run=args.dry_run,
                                   idempotent=args.idempotent)
    except ToolError as err:
        print(str(err), file=sys.stderr)
        return 1
    if args.json:
        combined = {}
        if res_lifecycle:
            combined["lifecycle"] = res_lifecycle
        if res_worklog:
            combined["worklog"] = res_worklog
        print(json.dumps(combined if len(combined) > 1 else (res_worklog or res_lifecycle), ensure_ascii=False, indent=2))
    else:
        prefix = "[dry-run] " if args.dry_run else ""
        if res_lifecycle:
            if res_lifecycle["changes"]:
                print(f"{prefix}lifecycle {res_lifecycle['from']} → {res_lifecycle['to']} " + " ".join(f"{k}={v}" for k, v in res_lifecycle["changes"].items() if k != "lifecycle"))
            elif args.log_time is None:
                print(f"lifecycle={res_lifecycle['from']} 无需变更")
        if res_worklog:
            st = res_worklog["status"]
            ent = res_worklog["entry"]
            if st == "added":
                print(f"{prefix}已登记工时: {ent['date']} [{ent['stage']}] {ent['spent']}h - {ent['summary']} ({ent['operator']}) | 累计: {res_worklog['total']}h")
            elif st == "existing":
                print(f"{prefix}工时已存在（幂等放行）: {ent['date']} [{ent['stage']}] {ent['spent']}h | 累计: {res_worklog['total']}h")
    return 0


if __name__ == "__main__":
    sys.exit(main())
