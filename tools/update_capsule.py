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
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ORDER = ("draft", "active", "delivered", "archived", "archived-unmeasured")
TERMINAL = ("archived", "archived-unmeasured")
REVIEW_DAYS, CLOSURE_DAYS = 30, 60
BLOCKING = ("Critical", "Major")


class ToolError(Exception):
    """可恢复业务异常，包含行动导向修复指引。"""


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
    parser = argparse.ArgumentParser(description="胶囊生命周期推进：按证据更新 capsule.yaml 的 lifecycle 与交付日期")
    parser.add_argument("capsule", help="胶囊目录（含 capsule.yaml）")
    parser.add_argument("--delivered", nargs="?", const=date.today().isoformat(), metavar="YYYY-MM-DD",
                        help="标记已交付（代码合入主干且全量测试通过），省略日期取今天")
    parser.add_argument("--dry-run", action="store_true", help="只输出将要做的变更，不写入")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()
    try:
        res = update_capsule(args.capsule, delivered=args.delivered, dry_run=args.dry_run)
    except ToolError as err:
        print(str(err), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif res["changes"]:
        prefix = "[dry-run] " if res["dry_run"] else ""
        print(f"{prefix}lifecycle {res['from']} → {res['to']} " + " ".join(f"{k}={v}" for k, v in res["changes"].items() if k != "lifecycle"))
    else:
        print(f"lifecycle={res['from']} 无需变更")
    return 0


if __name__ == "__main__":
    sys.exit(main())
