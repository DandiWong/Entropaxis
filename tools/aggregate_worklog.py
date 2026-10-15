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
                        })
                seg_task_ids = []
                for seg in data.get("task_segments") or []:
                    if isinstance(seg, dict) and seg.get("taskId"):
                        seg_task_ids.append(str(seg["taskId"]))
                return {
                    "id": str(data.get("id") or ""),
                    "topic": str(data.get("topic") or path.parent.name),
                    "lifecycle": str(data.get("lifecycle") or "draft"),
                    "worklogs": clean_logs,
                    "task_ids": seg_task_ids,
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
                "source": "orphan_trailer",
                "repo": str(repo),
            })

    total_hours = round(sum(i["spent"] for i in items), 2)
    return {
        "since": since,
        "until": until,
        "total_hours": total_hours,
        "items": items,
        "warnings": warnings,
    }


def format_markdown(data: dict[str, Any], by: str = "project") -> str:
    """格式化输出 Markdown 表格。"""
    lines = [
        f"### 工时统计汇总（{data['since']} ~ {data['until']}）",
        f"**总工时**: **{data['total_hours']}h**\n",
    ]

    items = data["items"]
    if not items:
        lines.append("当前时间段内无登记工时。")
        return "\n".join(lines)

    lines.append("| 项目 | Task-ID | 主题 / 阶段 | 工时(h) | 来源 | 说明 | 登记人 |")
    lines.append("|---|---|---|---|---|---|---|")

    # 排序：项目、日期
    sorted_items = sorted(items, key=lambda x: (x["project"], x["task_id"], x["date"]))
    for it in sorted_items:
        src_tag = "worklog" if it["source"] == "worklog" else "⚠️ 孤儿Trailer"
        lines.append(
            f"| `{it['project']}` | `{it['task_id']}` | {it['topic']} ({it['stage']}) | "
            f"{it['spent']} | {src_tag} | {it['summary']} | {it['operator']} |"
        )
    lines.append(f"| **合计** | | | **{data['total_hours']}** | | | |")

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
    parser.add_argument("--by", choices=["project", "task", "day"], default="project", help="聚合维度")
    parser.add_argument("--format", choices=["md", "json"], default="md", help="输出格式")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="工作区根目录")
    args = parser.parse_args()

    root = _find_workspace_root(args.root)
    data = aggregate(
        root=root,
        since=args.since,
        until=args.until,
        project_filter=args.project,
        operator_filter=args.operator,
    )

    if args.format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(format_markdown(data, by=args.by))
    return 0


if __name__ == "__main__":
    sys.exit(main())
