#!/usr/bin/env python3
"""发现目录职责与归属候选；默认只读，显式选择后增量登记。"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import sys
from pathlib import Path

try:
    from . import paths, project_registry
except ImportError:
    import paths
    import project_registry

CODE_INDICATORS = ("Cargo.toml", "pyproject.toml", "package.json", "go.mod", "pom.xml",
                   "build.gradle", "project.yml", "requirements.txt", "CMakeLists.txt", "Makefile")
SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "target", "__pycache__", "archive", "output"}
KB_KEYWORDS = ("asset", "kb", "knowledge", "资料", "知识库", "文献", "标准", "图谱", "档案")
PROJECT_MARKERS = ("01_项目管理", "02_业务运营", "03_工程研发", "DECISIONS.md", "PRODUCT.md")


def _config(root: Path) -> dict:
    file = project_registry.registry_path(root).with_name("workspace-config.yaml")
    if not file.exists():
        return {}
    data = project_registry._yaml().safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("workspace-config.yaml 必须为映射")
    return data


def _excluded(relative: str, patterns: list[str]) -> bool:
    parts = relative.strip("/").split("/")
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    for pattern in patterns:
        pattern = pattern.rstrip("/")
        variants = [pattern, pattern[3:]] if pattern.startswith("**/") else [pattern]
        if any(fnmatch.fnmatchcase(p, v) for p in prefixes for v in variants):
            return True
    return False


def is_code_repo(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and (
        any((path / name).is_file() for name in CODE_INDICATORS)
        or any(path.glob("*.xcodeproj")) or any(path.glob("*.xcworkspace")))


def discover_sub_code(project_dir: Path, *, root: Path | None = None,
                      excludes: list[str] | None = None) -> list[str]:
    """最多三层；工程内仅进入通用工程容器，跳过依赖、产物和软链接。"""
    root = root or project_dir.parent
    excludes = excludes or []
    found = []
    containers = {"apps", "packages", "03_工程研发", "source", "src"}
    def walk(directory, depth):
        relative = directory.relative_to(root).as_posix()
        if directory.is_symlink() or _excluded(relative, excludes):
            return
        code = is_code_repo(directory)
        if code:
            found.append(relative)
        if depth >= 3:
            return
        for child in sorted(directory.iterdir()):
            if not child.is_dir() or child.is_symlink() or child.name.startswith(".") or child.name.lower() in SKIP_DIRS:
                continue
            if code and child.name not in containers and not child.name.startswith("impl-"):
                continue
            walk(child, depth + 1)
    walk(project_dir, 0)
    return found


def is_knowledge_base(path: Path) -> tuple[bool, str]:
    hint = any(k in path.name.lower() for k in KB_KEYWORDS)
    if hint and (path / "index.md").is_file():
        return True, "知识目录名称与 index.md 索引；共享作用域需确认"
    return False, ""


def should_exclude(path: Path) -> tuple[bool, str]:
    # 精确目录名，不用子串匹配，避免 reporting/template 等误判。
    if path.name.lower() in SKIP_DIRS:
        return True, "依赖、构建产物或归档目录名称；排除需确认"
    return False, ""


def _project(path: Path, root: Path, used_ids: set[str], excludes: list[str], evidence: list[str]) -> dict:
    relative = path.relative_to(root).as_posix()
    base = re.sub(r"[^a-z0-9_-]+", "-", path.name.lower()).strip("-") or "project"
    project_id = base
    if base == "project" or base in used_ids:
        project_id = base + "-" + hashlib.sha256(relative.encode()).hexdigest()[:8]
    if project_id in used_ids:
        raise ValueError(f"项目 ID 冲突: {relative}")
    used_ids.add(project_id)
    knowledge = [p.relative_to(root).as_posix() for p in sorted(path.iterdir())
                 if p.is_dir() and not p.is_symlink()
                 and ("知识库" in p.name or is_knowledge_base(p)[0])
                 and not _excluded(p.relative_to(root).as_posix(), excludes)]
    return {"id": project_id, "name": path.name, "path": relative,
            "aliases": [path.name], "code": discover_sub_code(path, root=root, excludes=excludes),
            "knowledge": knowledge, "evidence": evidence}


def scan_workspace(root: Path) -> dict[str, list[dict]]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"工作区目录不存在: {root}")
    projects = project_registry.load_projects(root)
    excludes = project_registry.load_excludes(root)
    shared = {s["dir"].rstrip("/") for s in _config(root).get("shared_dirs") or []}
    known_paths = {p["path"] for p in projects}
    used_ids = {p["id"] for p in projects}
    result = {key: [] for key in ("projects", "shared_kbs", "excludes", "updates", "pending")}
    for p in projects:
        directory = root / p["path"]
        if directory.is_symlink() or not directory.resolve().is_relative_to(root):
            result["pending"].append({"path": p["path"], "reason": "已登记项目为软链接或越过工作区边界"})
            continue
        if p["path"] in shared or _excluded(p["path"], excludes):
            result["pending"].append({"path": p["path"], "reason": "项目登记与共享/排除声明冲突"})
            continue
        if directory.is_dir():
            child_boundaries = [other["path"] for other in projects if other["path"].startswith(p["path"] + "/")]
            new_code = [c for c in discover_sub_code(directory, root=root, excludes=excludes + child_boundaries) if c not in p["code"]]
            if new_code:
                result["updates"].append({"id": p["id"], "path": p["path"], "code": new_code,
                                           "evidence": ["已登记项目内发现新增构建入口"]})
    for item in sorted(root.iterdir()):
        if item.name.startswith(".") or not item.is_dir():
            continue
        name = item.name
        if item.is_symlink():
            result["pending"].append({"path": name, "reason": "软链接不扫描，须人工确认边界"})
            continue
        if name in known_paths or name in shared or _excluded(name, excludes):
            continue
        # 已登记嵌套项目的上层容器不重复登记。
        if any(p.startswith(name + "/") for p in known_paths | shared):
            continue
        excluded, exclusion_reason = should_exclude(item)
        if excluded:
            result["excludes"].append({"name": name, "path": name + "/", "reason": exclusion_reason})
            continue
        code = discover_sub_code(item, root=root, excludes=excludes)
        project_markers = [m for m in PROJECT_MARKERS if (item / m).exists()]
        kb, reason = is_knowledge_base(item)
        if kb and (code or project_markers):
            result["pending"].append({"path": name, "reason": "项目/工程与知识索引证据并存，需确认主职责"})
        elif code or project_markers:
            evidence = (["构建入口: " + ", ".join(code)] if code else []) + project_markers
            result["projects"].append(_project(item, root, used_ids, excludes, evidence))
        elif kb:
            result["shared_kbs"].append({"name": name, "dir": name + "/", "purpose": reason})
        else:
            hint = "名称提示资料目录；作用域需确认" if any(k in name.lower() for k in KB_KEYWORDS) else "缺少明确项目、工程或知识索引证据"
            result["pending"].append({"path": name, "reason": hint})
    return result


def register_scanned(root: Path, scanned: dict[str, list[dict]], apply: bool = False) -> bool:
    if not apply:
        return False
    entries = []
    for p in scanned.get("projects", []):
        entry = {k: p[k] for k in ("id", "name", "aliases")}
        entry["path"] = p["path"].rstrip("/") + "/"
        if p.get("code"):
            entry["code"] = p["code"]
        entry["note"] = "扫描候选经显式选择登记"
        entries.append(entry)
    shared = [{"dir": k["dir"], "purpose": k["purpose"]} for k in scanned.get("shared_kbs", [])]
    excludes = [e["path"] for e in scanned.get("excludes", [])] + [s["dir"] for s in shared]
    return project_registry.merge_scanned(root, entries, excludes, shared, scanned.get("updates", []))


def _select(root, scanned, selected, kind):
    plan = {k: [] for k in scanned}
    registered = {p["path"] for p in project_registry.load_projects(root)}
    shared = {s["dir"].rstrip("/") for s in _config(root).get("shared_dirs") or []}
    excludes = project_registry.load_excludes(root)
    ids = {p["id"] for p in project_registry.load_projects(root)}
    for path in dict.fromkeys(selected):
        path = path.rstrip("/")
        directory = root / path
        if not path or Path(path).is_absolute() or ".." in Path(path).parts or directory.is_symlink() or not directory.resolve().is_relative_to(root) or not directory.is_dir():
            raise ValueError(f"所选目录无效或越界: {path}")
        matches = [(k, item) for k, items in scanned.items() for item in items
                   if item.get("path", item.get("dir", "")).rstrip("/") == path]
        if any(k == "pending" and (path in registered or directory.is_symlink()) for k, _ in matches):
            raise ValueError(f"声明冲突须先人工修正: {path}")
        if not matches:
            if path in registered or path in shared or _excluded(path, excludes):
                continue  # 已生效的选择保持幂等；不改判人工声明。
            raise ValueError(f"目录不在候选中: {path}")
        if kind:
            if path in registered or path in shared or _excluded(path, excludes):
                raise ValueError(f"已有声明不能通过扫描改判: {path}")
            if kind == "project":
                plan["projects"].append(_project(directory, root, ids, excludes, ["人工指定项目职责"]))
            elif kind == "shared":
                plan["shared_kbs"].append({"dir": path + "/", "purpose": "人工确认跨项目共享资料"})
            else:
                plan["excludes"].append({"path": path + "/"})
        else:
            for k, item in matches:
                if k == "pending":
                    raise ValueError(f"目录职责待确认，请用 --kind 指定: {path}")
                plan[k].append(item)
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=paths.WORKSPACE_ROOT)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="登记明确选择的候选")
    action.add_argument("--dry-run", action="store_true", help="只读预览（默认）")
    parser.add_argument("--select", action="append", default=[], help="确认的工作区相对目录，可重复")
    parser.add_argument("--kind", choices=("project", "shared", "exclude"), help="人工指定单个目录职责")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if args.limit < 1 or args.offset < 0:
            raise ValueError("limit 须为正数，offset 须非负")
        if args.apply and not args.select:
            raise ValueError("--apply 必须配合 --select 明确选择目录")
        if args.kind and len(args.select) != 1:
            raise ValueError("--kind 必须配合一个 --select")
        root = args.root.resolve()
        scanned = scan_workspace(root)
        plan = _select(root, scanned, args.select, args.kind) if args.select else scanned
        if args.apply:
            register_scanned(root, plan, apply=True)
        counts = {k: len(v) for k, v in plan.items()}
        page = {k: v[args.offset:args.offset + args.limit] for k, v in plan.items()}
        if args.json:
            print(json.dumps({"applied": args.apply, "counts": counts, "offset": args.offset,
                              "limit": args.limit, "candidates": page}, ensure_ascii=False))
        else:
            print("status=" + ("applied" if args.apply else "preview") + " " + " ".join(f"{k}={v}" for k, v in counts.items()))
            for key, items in page.items():
                for item in items:
                    print(f"{key}: {item.get('path', item.get('dir'))} | {item.get('reason', item.get('purpose', ', '.join(item.get('evidence', []))))}")
            if any(v > args.offset + args.limit for v in counts.values()):
                print(f"next_offset={args.offset + args.limit}")
        return 0
    except (OSError, ValueError, KeyError, TypeError, project_registry._yaml().YAMLError) as exc:
        print(f"❌ 扫描或登记失败: {exc}\n👉 检查路径、配置及选择；先运行 --dry-run --json 预览。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
