#!/usr/bin/env python3
"""nightly 回归作业注册表的唯一读写入口（list/add/remove/enable + status）。

实例数据落在 `data/templates/nightly.yaml`（merge-only，契约 schemas/nightly.schema.json），
不进 Skill 目录——Skill 只做编排，作业实例是工作区本地事实。
凭证不在此登记：wrapper 从项目 .env 或环境变量读，缺凭据记 SKIP 不记 FAIL。

设计不变量:
  1. merge-only：只追加/改字段，整文件重写必须带 --force 且由用户显式触发；
  2. 唯一性：id 与 label 全局唯一，冲突即拒，不静默改名；
  3. 原子写入：临时文件 + os.replace；写入后重新解析复核，不符即放弃；
  4. 状态只读探测：status 子命令跑 launchctl 与报告目录，不注册不卸载；
  5. 默认静默高密度：list 单行一作业。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from . import paths
    from . import project_registry
    from . import validate_schema
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import paths  # type: ignore
    import project_registry  # type: ignore
    import validate_schema  # type: ignore

REGISTRY_NAME = "nightly.yaml"
LABEL_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
VALID_CARRIERS = ("launchd", "ci")
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"


class NightlyError(Exception):
    """可恢复业务异常，包含行动导向修复指引。"""


def registry_path(root: Path) -> Path:
    return root / paths.SYSTEM_DIRNAME / "data" / "templates" / REGISTRY_NAME


def _yaml():
    try:
        import yaml  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise NightlyError("❌ 读取 nightly 注册表需要 PyYAML\n👉 pip install pyyaml") from exc
    return yaml


def load(root: Path) -> dict:
    p = registry_path(root)
    if not p.is_file():
        raise NightlyError(
            f"❌ 未找到 nightly 注册表: {p}\n"
            "👉 修复建议: 运行 `python3 .entropaxis/tools/bootstrap.py` 从模板渲染，"
            "或从 templates/instance/nightly.template.yaml 复制后补 _meta 标记。"
        )
    yaml = _yaml()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise NightlyError(f"❌ nightly 注册表不是合法 YAML: {exc}\n👉 修复建议: 修正语法后重试。") from exc
    if not isinstance(data, dict):
        raise NightlyError("❌ nightly 注册表顶层必须是映射\n👉 修复建议: 参照 templates/instance/nightly.template.yaml。")
    errs = validate_schema.validate(data, validate_schema.load_schema("nightly"))
    if errs:
        raise NightlyError(
            f"❌ nightly 注册表未通过 schemas/nightly.schema.json: {'；'.join(errs[:3])}\n"
            "👉 修复建议: 按契约补必填字段（id/project/label/schedule/commands）。"
        )
    return data


def save(root: Path, data: dict) -> None:
    """整文件写回前先过 schema；调用方负责保证只做了字段级增量。"""
    errs = validate_schema.validate(data, validate_schema.load_schema("nightly"))
    if errs:
        raise NightlyError(f"❌ 拒绝写入：数据未通过 schema: {'；'.join(errs[:3])}")
    yaml = _yaml()
    p = registry_path(root)
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=120)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


def _resolve_repo(root: Path, project_id: str, explicit: str | None) -> str:
    if explicit:
        return explicit.rstrip("/")
    for p in project_registry.load_projects(root):
        if p["id"] == project_id:
            if p.get("code"):
                return p["code"][0].rstrip("/")
            raise NightlyError(
                f"❌ 项目 {project_id} 未在 registry 登记 code 目录，无法推断 repo\n"
                "👉 修复建议: 用 --repo 显式指定，或先在 registry.yaml 补 code。"
            )
    raise NightlyError(
        f"❌ 项目 {project_id} 不在 registry.yaml 中\n"
        "👉 修复建议: 先用 resolve_project.py 查正确 id，或先立项登记。"
    )


def add(root: Path, args: argparse.Namespace) -> int:
    data = load(root)
    jobs = data.get("jobs") or []
    for j in jobs:
        if j.get("id") == args.id:
            raise NightlyError(f"❌ 作业 id 已存在: {args.id}\n👉 修复建议: 换个 id，或先 remove 旧作业。")
        if j.get("label") == args.label:
            raise NightlyError(f"❌ launchd label 已被 {j['id']} 占用: {args.label}\n👉 修复建议: label 须全局唯一。")
    if not LABEL_RE.match(args.label):
        raise NightlyError(f"❌ label 含非法字符: {args.label!r}\n👉 修复建议: 只允许字母、数字、点、下划线、连字符。")
    if args.carrier not in VALID_CARRIERS:
        raise NightlyError(f"❌ carrier 只能是 {VALID_CARRIERS}\n👉 修复建议: launchd 为过渡，ci 为终态。")

    entry: dict = {
        "id": args.id,
        "project": args.project,
        "label": args.label,
        "carrier": args.carrier,
        "schedule": {"hour": args.hour, "minute": args.minute},
        "commands": args.commands,
    }
    if args.weekday is not None:
        entry["schedule"]["weekday"] = args.weekday
    entry["repo"] = _resolve_repo(root, args.project, args.repo)
    if args.report_dir:
        entry["report_dir"] = args.report_dir
    if args.disabled:
        entry["enabled"] = False
    if args.note:
        entry["note"] = args.note

    jobs.append(entry)
    data["jobs"] = jobs
    save(root, data)
    print(f"✅ 已登记 {args.id}（{args.project} · {args.carrier} · {args.hour:02d}:{args.minute:02d}）")
    if entry.get("enabled", True) and args.carrier == "launchd":
        print("👉 下一步: 生成 wrapper 与 plist 后 `launchctl bootstrap gui/$(id -u) <plist>`；")
        print("   凭证由项目 .env 提供，不要写进注册表。")
    return 0


def remove(root: Path, args: argparse.Namespace) -> int:
    data = load(root)
    jobs = data.get("jobs") or []
    kept = [j for j in jobs if j.get("id") != args.id]
    if len(kept) == len(jobs):
        known = ", ".join(j.get("id", "?") for j in jobs) or "（空）"
        raise NightlyError(f"❌ 作业不存在: {args.id}\n👉 修复建议: 现有作业: {known}")
    data["jobs"] = kept
    save(root, data)
    print(f"✅ 已移除 {args.id}")
    plist = PLIST_DIR / f"{next((j['label'] for j in jobs if j.get('id') == args.id), '')}.plist"
    if plist.is_file():
        print(f"⚠️ 本机仍有 plist: {plist}")
        print(f"👉 卸载: launchctl bootout gui/$(id -U) {plist.stem} && rm {plist}")
    return 0


def set_enabled(root: Path, args: argparse.Namespace) -> int:
    data = load(root)
    hit = None
    for j in data.get("jobs") or []:
        if j.get("id") == args.id:
            j["enabled"] = args.enable
            hit = j
            break
    if hit is None:
        known = ", ".join(j.get("id", "?") for j in (data.get("jobs") or [])) or "（空）"
        raise NightlyError(f"❌ 作业不存在: {args.id}\n👉 修复建议: 现有作业: {known}")
    save(root, data)
    state = "启用" if args.enable else "停用"
    print(f"✅ {args.id} 已{state}")
    if not args.enable:
        print(f"👉 若已注册 launchd，执行: launchctl bootout gui/$(id -U) {hit['label']}")
    return 0


def list_jobs(root: Path) -> int:
    data = load(root)
    jobs = data.get("jobs") or []
    if not jobs:
        print("（无 nightly 作业；用 add 子命令登记）")
        return 0
    print(f"{'ID':<18}{'项目':<20}{'承载':<9}{'时间':<8}{'状态':<6}label")
    for j in jobs:
        sch = j.get("schedule", {})
        wd = sch.get("weekday")
        when = f"{sch.get('hour', 0):02d}:{sch.get('minute', 0):02d}"
        if wd is not None:
            when += f"/w{wd}"
        enabled = "on" if j.get("enabled", True) else "off"
        print(f"{j['id']:<18}{j.get('project', ''):<20}{j.get('carrier', ''):<9}{when:<8}{enabled:<6}{j.get('label', '')}")
    print(f"# {len(jobs)} 个作业 · 本机 plist 目录: {PLIST_DIR}")
    return 0


def status_rows(root: Path) -> list[dict]:
    """只读探测：注册表 × 本机 launchd × 最近报告，三方对齐。"""
    data = load(root)
    projects = {p["id"]: p for p in project_registry.load_projects(root)}
    rows = []
    for j in data.get("jobs") or []:
        label = j.get("label", "")
        plist = PLIST_DIR / f"{label}.plist"
        loaded = False
        if plist.is_file():
            proc = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                                  capture_output=True, text=True)
            loaded = proc.returncode == 0
        repo_rel = j.get("repo") or (projects.get(j.get("project"), {}).get("code") or [None])[0]
        report_dir = (root / repo_rel / j.get("report_dir", "docs/05_reports/")) if repo_rel else None
        latest = None
        if report_dir and report_dir.is_dir():
            cands = sorted(report_dir.glob("*nightly*"), key=lambda p: p.stat().st_mtime, reverse=True)
            latest = cands[0].name if cands else None
        rows.append({
            "id": j.get("id"), "project": j.get("project"), "carrier": j.get("carrier"),
            "label": label, "enabled": bool(j.get("enabled", True)),
            "plist": str(plist) if plist.is_file() else None, "loaded": loaded,
            "repo": repo_rel, "latest_report": latest,
        })
    return rows


def status(root: Path) -> int:
    rows = status_rows(root)
    if not rows:
        print("（无 nightly 作业）")
        return 0
    print(f"{'ID':<18}{'项目':<20}{'plist':<7}{'loaded':<7}{'启用':<5}最近报告")
    for r in rows:
        print(f"{r['id']:<18}{r['project'] or '':<20}"
              f"{'有' if r['plist'] else '无':<7}{'是' if r['loaded'] else '否':<7}"
              f"{'on' if r['enabled'] else 'off':<5}{r['latest_report'] or '—'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="nightly 回归作业注册表：登记哪些项目、跑什么、何时跑（实例数据在 data/）")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="工作区根，默认当前目录")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出全部作业（默认静默高密度）")
    sub.add_parser("status", help="只读探测注册表 × launchd × 报告三方状态")

    p_add = sub.add_parser("add", help="登记一个作业")
    p_add.add_argument("--id", required=True, help="作业标识，小写连字符")
    p_add.add_argument("--project", required=True, help="registry.yaml 项目 id")
    p_add.add_argument("--label", required=True, help="launchd label，如 com.org.proj.nightly")
    p_add.add_argument("--hour", type=int, required=True, help="小时 0-23，默认避开工作时间")
    p_add.add_argument("--minute", type=int, default=0, help="分钟 0-59")
    p_add.add_argument("--weekday", type=int, help="0/7 周日，缺省每天")
    p_add.add_argument("--carrier", default="launchd", choices=list(VALID_CARRIERS))
    p_add.add_argument("--repo", help="代码真源目录，相对工作区根；缺省取 registry code[0]")
    p_add.add_argument("--commands", nargs="+", required=True, help="wrapper 内顺序执行的命令原文")
    p_add.add_argument("--report-dir", help="报告目录，相对 repo，缺省 docs/05_reports/")
    p_add.add_argument("--note")
    p_add.add_argument("--disabled", action="store_true", help="只登记不注册")

    p_rm = sub.add_parser("remove", help="移除一个作业")
    p_rm.add_argument("id")

    p_en = sub.add_parser("enable", help="启用作业")
    p_en.add_argument("id")
    p_dis = sub.add_parser("disable", help="停用作业（保留登记）")
    p_dis.add_argument("id")

    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    try:
        if args.cmd == "add":
            return add(root, args)
        if args.cmd == "remove":
            return remove(root, args)
        if args.cmd == "enable":
            args.enable = True
            return set_enabled(root, args)
        if args.cmd == "disable":
            args.enable = False
            return set_enabled(root, args)
        if args.cmd == "list":
            return list_jobs(root)
        if args.cmd == "status":
            return status(root)
    except NightlyError as err:
        print(str(err), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
