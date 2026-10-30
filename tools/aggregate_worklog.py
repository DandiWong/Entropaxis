#!/usr/bin/env python3
"""跨项目工时聚合器：按时间窗汇总工作区各项目胶囊 worklogs 与孤儿 Git Trailer。

真源契约: data/docs/20261012_跨项目工时统计与汇报方案/02_方案.md (approved) & 04_Spec_Tech-120.md
特性 ID: Tech-120 (G-3 跨项目聚合，两阶段防双计与派生抑制)

设计不变量:
  1. 纯只读：不落盘、不建外部索引、不修改任何现有文件与 Git 提交。
  2. 严格两阶段防双计（M-2）：
     - 阶段一：扫描全域建立 Known_Task_IDs 符号表；
     - 阶段二：凡属于 Known_Task_IDs 的 commit Trailer 一律视为派生副本完全抑制（无论窗内工时是否为 0）；
       仅真正找不到任何胶囊的 commit 才按 CommitDate 作为孤儿补位（打标 orphan_trailer）。
  3. 类型宽容与 Schema 一致性（M-1）：读取 YAML 若遇 datetime.date 对象，自动转为 isoformat 字符串。
  4. 坏数据容错：遇到损坏或违例条目只记录 warnings，不中断整体汇总。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from . import paths
    from . import project_registry
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import paths  # type: ignore
    import project_registry  # type: ignore

try:
    import yaml
except ImportError:
    yaml = None  # 宽容降级


def _find_workspace_root(cwd: Path) -> Path:
    cur = cwd.resolve()
    for p in [cur, *cur.parents]:
        if (p / ".entropaxis").is_dir():
            return p
    return cur


def _default_since() -> str:
    today = dt.date.today()
    # 缺省为本周一（ISO 周一）
    monday = today - dt.timedelta(days=today.weekday())
    return monday.isoformat()


def _parse_hours(val: Any) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip().rstrip("hH")
        return float(val)
    raise ValueError(f"无法解析工时: {val}")


def _entry_seconds(entry: dict[str, Any]) -> int:
    """worklog 有效秒：有 `seconds`（整数真源）就用它，旧条目按 round(spent×3600) 补算。

    方案 4.4 判定 10、五「新旧条目混存的汇总口径」：旧条目不改写、不推断 actor。
    """
    seconds = entry.get("seconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        return max(0, round(float(seconds)))
    try:
        return max(0, round(_parse_hours(entry.get("spent", 0)) * 3600))
    except (TypeError, ValueError):
        return 0


def _fmt_hms(seconds: int) -> str:
    """整数秒 → H:MM:SS（时不过零填充）；T12 汇总层统一显示口径。"""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _segment_machine_seconds(seg: dict[str, Any], since: str | None = None,
                             until: str | None = None) -> int | None:
    """段机时：有 `machine_seconds` 用它；旧段用 endAt − startAt（无 endAt 的 open 段不计）。

    只统计 actor=role 且 state ∈ {logged, failed} 的段（方案 2.2 G-4：失败计机时不计
    有效工时）；旧段（无 actor）一律不计入机时，不推断。窗口按段 `startAt` 日期取
    （方案 4.4 判定 10）——一段的机时归属它的开始日，不是结束日。
    """
    if not isinstance(seg, dict):
        return None
    if seg.get("actor") != "role" or seg.get("state") not in ("logged", "failed"):
        return None
    start, end = seg.get("startAt"), seg.get("endAt")
    if since or until:
        try:
            day = dt.datetime.fromisoformat(str(start)).date().isoformat()
        except (TypeError, ValueError):
            return None
        if since and day < since:
            return None
        if until and day > until:
            return None
    machine = seg.get("machine_seconds")
    if isinstance(machine, (int, float)) and not isinstance(machine, bool):
        return max(1, round(float(machine)))
    if not start or not end:
        return None
    try:
        span = (dt.datetime.fromisoformat(str(end))
                - dt.datetime.fromisoformat(str(start))).total_seconds()
    except (TypeError, ValueError):
        return None
    return max(1, round(span))


def parse_capsule_file(path: Path) -> dict[str, Any] | None:
    """读取胶囊文件，提取 id, topic, worklogs。带日期类型适配。"""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    if yaml is not None:
        try:
            data = yaml.safe_load(content) or {}
            if isinstance(data, dict):
                raw_logs = data.get("worklogs") or []
                clean_logs = []
                for item in raw_logs:
                    if isinstance(item, dict):
                        d = item.get("date")
                        if isinstance(d, (dt.date, dt.datetime)):
                            d = d.strftime("%Y-%m-%d")
                        elif d is not None:
                            d = str(d).strip().strip("'\"")
                        clean_logs.append({
                            "date": d,
                            "stage": str(item.get("stage") or "misc"),
                            "spent": _parse_hours(item.get("spent", 0)),
                            "summary": str(item.get("summary") or ""),
                            "operator": str(item.get("operator") or ""),
                            # T12：只加不删。旧条目（无 actor/seconds）三项为 null/补算值，
                            # 新条目带 actor/role/seconds 供下游（Tech-175 P6 看板）读取。
                            "seconds": _entry_seconds(item),
                            "actor": (str(item.get("actor"))
                                      if item.get("actor") in ("role", "manager", "human") else None),
                            "role": str(item.get("role")) if item.get("role") else None,
                        })
                seg_task_ids = []
                role_segments = [s for s in (data.get("task_segments") or []) if isinstance(s, dict)]
                for seg in role_segments:
                    if seg.get("taskId"):
                        seg_task_ids.append(str(seg["taskId"]))
                return {
                    "id": str(data.get("id") or ""),
                    "topic": str(data.get("topic") or path.parent.name),
                    "lifecycle": str(data.get("lifecycle") or "draft"),
                    "worklogs": clean_logs,
                    "task_ids": seg_task_ids,
                    "role_segments": role_segments,
                    "path": path.parent,
                }
        except Exception:
            pass

    # 简易正则后备
    cid, topic = "", path.parent.name
    for line in content.splitlines():
        if line.startswith("id:"):
            cid = line.split(":", 1)[1].strip()
        elif line.startswith("topic:"):
            topic = line.split(":", 1)[1].strip().strip("'\"")
    return {
        "id": cid,
        "topic": topic,
        "lifecycle": "draft",
        "worklogs": [],
        "task_ids": [],
        "role_segments": [],
        "path": path.parent,
    }


def scan_capsules(root: Path, projects: list[dict], excludes: list[str]) -> list[dict[str, Any]]:
    """全域扫描胶囊清单。"""
    search_dirs: list[Path] = [root / ".entropaxis" / "data" / "docs"]
    for p in projects:
        p_path = root / p["path"]
        if p_path.is_dir():
            search_dirs.append(p_path)
    if not projects:
        search_dirs.append(root)
    found_files: set[Path] = set()
    capsules: list[dict[str, Any]] = []

    for base in search_dirs:
        if not base.is_dir():
            continue
        for cap in base.rglob("capsule.yaml"):
            if cap in found_files:
                continue
            # 检查是否匹配 exclude
            rel_str = str(cap.relative_to(root)) if cap.is_relative_to(root) else str(cap)
            if any(re.search(rf"\b{re.escape(ex)}\b", rel_str) for ex in excludes if ex):
                continue
            found_files.add(cap)
            parsed = parse_capsule_file(cap)
            if parsed and parsed.get("id"):
                capsules.append(parsed)

    return capsules


def attribute_project(capsule_path: Path, root: Path, projects: list[dict]) -> str:
    """根据胶囊物理路径匹配所属项目 ID。"""
    try:
        rel = capsule_path.resolve().relative_to(root.resolve())
        rel_str = str(rel)
    except ValueError:
        rel_str = str(capsule_path)

    if rel_str.startswith(".entropaxis/data/docs") or rel_str.startswith(".entropaxis"):
        return "_root"

    # 最长前缀匹配
    matched_id = "_unregistered"
    max_len = -1
    for p in projects:
        ppath = p["path"]
        if rel_str.startswith(ppath) and len(ppath) > max_len:
            max_len = len(ppath)
            matched_id = p["id"]
    return matched_id


def scan_git_trailers(repo: Path, since: str, until: str) -> list[dict[str, Any]]:
    """扫描代码库指定日期范围内的 Git Commit Trailers。"""
    if not (repo / ".git").is_dir() and not repo.is_dir():
        return []

    # 闭区间：从 since 当天 00:00:00 到 until 当天 23:59:59
    since_arg = f"{since} 00:00:00"
    until_arg = f"{until} 23:59:59"
    # 使用 %x1e (记录分隔符) 和 %x1f (单元分隔符)，防止 trailers 自带换行切断记录
    cmd = [
        "git", "-C", str(repo), "log", "--no-merges",
        f"--since={since_arg}", f"--until={until_arg}",
        "--format=%x1e%H%x1f%an%x1f%as%x1f%(trailers:key=Task-ID,valueonly)%x1f%(trailers:key=Time-Spent,valueonly)%x1f%s"
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except (subprocess.SubprocessError, OSError):
        return []

    entries = []
    commits = [c for c in proc.stdout.split("\x1e") if c.strip()]
    for c in commits:
        parts = [p.strip() for p in c.split("\x1f")]
        if len(parts) >= 6:
            commit, author, cdate, task_id, spent_str, subject = parts[:6]
            if task_id and spent_str:
                try:
                    spent = _parse_hours(spent_str)
                    entries.append({
                        "commit": commit,
                        "date": cdate,
                        "author": author,
                        "task_id": task_id,
                        "spent": spent,
                        "summary": subject,
                        "repo": repo,
                    })
                except ValueError:
                    pass
    return entries


def aggregate(
    root: Path,
    since: str,
    until: str,
    project_filter: str | None = None,
    operator_filter: str | None = None,
) -> dict[str, Any]:
    """两阶段聚合入口。"""
    projects = project_registry.load_projects(root)
    excludes = project_registry.load_excludes(root)

    # 阶段一：全域扫描建立 Known_Task_IDs
    capsules = scan_capsules(root, projects, excludes)
    known_task_ids: set[str] = {c["id"] for c in capsules if c.get("id")}
    for c in capsules:
        for tid in c.get("task_ids", []):
            if tid:
                known_task_ids.add(tid)
    task_to_capsule: dict[str, dict[str, Any]] = {c["id"]: c for c in capsules if c.get("id")}

    warnings: list[str] = []
    items: list[dict[str, Any]] = []

    # 阶段二 A：从胶囊收集窗内工时（真源层）
    for cap in capsules:
        proj_id = attribute_project(cap["path"], root, projects)
        if project_filter and proj_id != project_filter:
            continue

        for log in cap.get("worklogs", []):
            ldate = log.get("date")
            if not ldate or not (since <= ldate <= until):
                continue
            if operator_filter and log.get("operator") != operator_filter:
                continue

            items.append({
                "project": proj_id,
                "task_id": cap["id"],
                "topic": cap["topic"],
                "date": ldate,
                "spent": log["spent"],
                "stage": log.get("stage", "misc"),
                "summary": log.get("summary", ""),
                "operator": log.get("operator", ""),
                # T12：items[] 只加不删——采集器（collect_agent_perf）照读 spent。
                "seconds": log.get("seconds", 0),
                "actor": log.get("actor"),
                "role": log.get("role"),
                "source": "worklog",
                "capsule": str(cap["path"]),
            })

    # 阶段二 B：从代码仓收集 Trailer（派生抑制与孤儿补位）
    code_repos: list[tuple[str, Path]] = []
    for p in projects:
        for cpath in p.get("code") or []:
            full_repo = root / cpath
            if (full_repo / ".git").is_dir():
                code_repos.append((p["id"], full_repo))

    # 根工作区代码仓
    if (root / ".git").is_dir() and not any(r[1] == root for r in code_repos):
        code_repos.append(("_root", root))

    # 补充：扫描工作区内未在 registry 声明的其他代码仓（孤儿仓库）
    known_repo_paths = {r[1].resolve() for r in code_repos}
    for git_dir in root.rglob(".git"):
        r_path = git_dir.parent.resolve()
        if r_path not in known_repo_paths:
            proj_id = attribute_project(r_path, root, projects)
            code_repos.append((proj_id, r_path))
            known_repo_paths.add(r_path)
    for proj_id, repo in code_repos:
        if project_filter and proj_id != project_filter:
            continue
        trailers = scan_git_trailers(repo, since, until)
        for t in trailers:
            tid = t["task_id"]
            if tid in known_task_ids:
                # 派生抑制：无论胶囊窗内是否为 0，绝对不把已知胶囊的 Trailer 当孤儿补入
                continue

            # 真正孤儿补位
            if operator_filter and t["author"] != operator_filter:
                continue

            warnings.append(f"发现未关联胶囊的提交工时（孤儿）: Commit {t['commit'][:8]} [{tid}] {t['spent']}h")
            items.append({
                "project": proj_id,
                "task_id": tid,
                "topic": f"孤儿提交 ({repo.name})",
                "date": t["date"],
                "spent": t["spent"],
                "stage": "code",
                "summary": t["summary"],
                "operator": t["author"],
                # 孤儿 Trailer 不是 worklog：无 actor、无 seconds 真源，按补算秒数列「未标注」。
                "seconds": max(0, round(t["spent"] * 3600)),
                "actor": None,
                "role": None,
                "source": "orphan_trailer",
                "repo": str(repo),
            })

    total_hours = round(sum(i["spent"] for i in items), 2)
    # T12 分列（方案 2.2 G-2/G-4）：有效工时按 worklog seconds（旧条目补算）；机时按
    # actor=role 且 state ∈ {logged, failed} 的段，窗口按段 startAt 日期取。
    effective_seconds = sum(i["seconds"] for i in items)
    by_actor = {"role": 0, "manager": 0, "human": 0, "unlabeled": 0}
    for it in items:
        actor = it.get("actor")
        by_actor[actor if actor in by_actor else "unlabeled"] += it["seconds"]
    machine_seconds = 0
    for cap in capsules:
        if project_filter is not None and attribute_project(
                cap["path"], root, projects) != project_filter:
            continue
        for seg in cap.get("role_segments", []):
            machine = _segment_machine_seconds(seg, since, until)
            if machine is not None:
                machine_seconds += machine
    return {
        "since": since,
        "until": until,
        "total_hours": total_hours,
        "total_seconds": effective_seconds,
        "effective_seconds": effective_seconds,
        "machine_seconds": machine_seconds,
        "by_actor": by_actor,
        "items": items,
        "warnings": warnings,
    }


def format_markdown(data: dict[str, Any], by: str = "project") -> str:
    """格式化输出 Markdown 表格。

    T12（方案 4.4 判定 1 显示）：一律显示 H:MM:SS（含 `0:52:00`，不含 `0.8667`）；
    JSON 仍保留 `total_hours` 供旧消费方。头部给「有效工时 / 机时 / actor 分列」三行
    （方案 4.4 判定 10）。`items[]` 只加不删：`spent` 照旧，新条目另有 actor/role/seconds。
    """
    total_s = data.get("total_seconds", 0)
    machine_s = data.get("machine_seconds", 0)
    by_actor = data.get("by_actor") or {}
    lines = [
        f"### 工时统计汇总（{data['since']} ~ {data['until']}）",
        f"**总工时**: **{_fmt_hms(total_s)}**（{data['total_hours']}h）",
        f"**有效工时**: **{_fmt_hms(total_s)}**",
        f"**机时**: **{_fmt_hms(machine_s)}**",
        "**actor 分列**: " + " · ".join(
            f"{name} {_fmt_hms(by_actor.get(name, 0))}"
            for name in ("role", "manager", "human", "unlabeled")) + "\n",
    ]

    items = data["items"]
    if not items:
        lines.append("当前时间段内无登记工时。")
        return "\n".join(lines)
    if by == "stage":
        stage_groups: dict[str, list[dict[str, Any]]] = {}
        for it in items:
            st = it.get("stage") or "misc"
            stage_groups.setdefault(st, []).append(it)

        lines.append("| 阶段 | 工时 | 占比 | 包含记录数 | 涉及任务 |")
        lines.append("|---|---|---|---|---|")
        for st in sorted(stage_groups.keys()):
            st_items = stage_groups[st]
            st_secs = sum(i["seconds"] for i in st_items)
            pct = f"{st_secs / total_s * 100:.1f}%" if total_s else "0.0%"
            tasks = "、".join(sorted({f"`{i['task_id']}`" for i in st_items if i.get("task_id")}))
            lines.append(f"| `{st}` | **{_fmt_hms(st_secs)}** | {pct} | {len(st_items)} | {tasks} |")
        lines.append(f"| **合计** | **{_fmt_hms(total_s)}** | 100.0% | {len(items)} | |")
    elif by == "task":
        task_groups: dict[str, list[dict[str, Any]]] = {}
        for it in items:
            tid = it.get("task_id") or "未关联"
            task_groups.setdefault(tid, []).append(it)

        lines.append("| Task-ID | 主题 | 阶段分布 | 工时 | 占比 |")
        lines.append("|---|---|---|---|---|")
        for tid in sorted(task_groups.keys()):
            t_items = task_groups[tid]
            t_secs = sum(i["seconds"] for i in t_items)
            pct = f"{t_secs / total_s * 100:.1f}%" if total_s else "0.0%"
            topic = t_items[0].get("topic", "")
            stages_summary = " · ".join(
                f"{s}:{_fmt_hms(sum(x['seconds'] for x in t_items if x.get('stage') == s))}"
                for s in sorted({x.get("stage", "misc") for x in t_items}))
            lines.append(f"| `{tid}` | {topic} | {stages_summary} | **{_fmt_hms(t_secs)}** | {pct} |")
        lines.append(f"| **合计** | | | **{_fmt_hms(total_s)}** | 100.0% |")
    elif by == "day":
        day_groups: dict[str, list[dict[str, Any]]] = {}
        for it in items:
            day = it.get("date") or "未知"
            day_groups.setdefault(day, []).append(it)

        lines.append("| 日期 | 工时 | 占比 | 阶段分布 | 涉及任务 |")
        lines.append("|---|---|---|---|---|")
        for day in sorted(day_groups.keys()):
            d_items = day_groups[day]
            d_secs = sum(i["seconds"] for i in d_items)
            pct = f"{d_secs / total_s * 100:.1f}%" if total_s else "0.0%"
            stages_summary = " · ".join(
                f"{s}:{_fmt_hms(sum(x['seconds'] for x in d_items if x.get('stage') == s))}"
                for s in sorted({x.get("stage", "misc") for x in d_items}))
            tasks = "、".join(sorted({f"`{i['task_id']}`" for i in d_items if i.get("task_id")}))
            lines.append(f"| `{day}` | **{_fmt_hms(d_secs)}** | {pct} | {stages_summary} | {tasks} |")
        lines.append(f"| **合计** | **{_fmt_hms(total_s)}** | 100.0% | | |")
    else:
        lines.append("| 项目 | Task-ID | 主题 / 阶段 | 工时 | actor | 来源 | 说明 | 登记人 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        sorted_items = sorted(items, key=lambda x: (x["project"], x["task_id"], x["date"]))
        for it in sorted_items:
            src_tag = "worklog" if it["source"] == "worklog" else "⚠️ 孤儿Trailer"
            actor = it.get("actor") or "未标注"
            if it.get("role"):
                actor = f"{actor}/{it['role']}"
            lines.append(
                f"| `{it['project']}` | `{it['task_id']}` | {it['topic']} ({it['stage']}) | "
                f"{_fmt_hms(it['seconds'])} | {actor} | {src_tag} | {it['summary']} | {it['operator']} |"
            )
        lines.append(f"| **合计** | | | **{_fmt_hms(total_s)}** | | | | |")
    if data.get("warnings"):
        lines.append("\n**⚠️ 审计告警区**:")
        for w in data["warnings"]:
            lines.append(f"- {w}")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="跨项目工时聚合与汇报生成工具（Tech-120）")
    parser.add_argument("--since", default=_default_since(), help="统计起始日期 YYYY-MM-DD（缺省本周一）")
    parser.add_argument("--until", default=dt.date.today().isoformat(), help="统计截止日期 YYYY-MM-DD（缺省今天）")
    parser.add_argument("--project", help="指定项目 ID 过滤")
    parser.add_argument("--operator", help="指定登记人过滤")
    parser.add_argument("--by", choices=["project", "task", "day", "stage"], default="project", help="聚合维度")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="工作区根目录")
    parser.add_argument("--json", action="store_true",
                        help="输出 JSON（含 seconds/total_seconds/effective_seconds/machine_seconds/by_actor）")
    args = parser.parse_args()

    root = _find_workspace_root(args.root)
    data = aggregate(
        root=root,
        since=args.since,
        until=args.until,
        project_filter=args.project,
        operator_filter=args.operator,
    )

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_markdown(data, by=args.by))
    return 0


if __name__ == "__main__":
    sys.exit(main())
