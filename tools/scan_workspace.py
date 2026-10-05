#!/usr/bin/env python3
"""自动扫描工作区根目录下的既有目录，识别项目、工程与知识库，并注册到 Entropaxis 配置中。

分类标准：
1. 知识库 / 共享资料 (shared_dirs):
   - 含有大量 pdf、文献、图谱设计、报告、课程素材、数据集，或目录名显式包含 asset/kb/knowledge/资料/知识库/文献。
   - 注册到 workspace-config.yaml 的 shared_dirs，并加入 registry.yaml 的 exclude 避免误报为未注册业务项目。
2. 排除目录 (exclude):
   - 工具安装包、归档、第三方依赖或临时目录（如 Archive, node_modules, repo 等）。
3. 业务项目与工程 (project & code):
   - 含有代码工程特征（.git, Cargo.toml, pyproject.toml, package.json, *.xcodeproj, project.yml 等）。
   - 含有文档/管理规范（AGENTS.md, README.md, docs/, spec.md, DESIGN.md 等）。
   - 项目注册到 registry.yaml 的 projects 中；其内部工程或子模块识别为 code 属性。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

try:
    from . import paths, project_registry
except ImportError:
    import paths
    import project_registry

KB_KEYWORDS = ("asset", "assets", "kb", "knowledge", "资料", "知识库", "文献", "标准", "图谱", "档案")
EXCLUDE_KEYWORDS = ("archive", "install", "installer", "setup", "node_modules", "output", "repo", "temp")
CODE_INDICATORS = (
    ".git",
    "Cargo.toml",
    "pyproject.toml",
    "package.json",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "project.yml",
    "requirements.txt",
    "CMakeLists.txt",
    "Makefile",
)


def _yaml():
    return project_registry._yaml()


def is_code_repo(path: Path) -> bool:
    """判断目录是否为代码工程或包含工程构建文件。"""
    if not path.is_dir():
        return False
    for ind in CODE_INDICATORS:
        if (path / ind).exists():
            return True
    if any(path.glob("*.xcodeproj")) or any(path.glob("*.xcworkspace")):
        return True
    return False


def discover_sub_code(project_dir: Path) -> list[str]:
    """发现项目内的代码工程真源目录（相对项目根目录）。"""
    code_dirs: list[str] = []
    # 1. 检查项目根自身是否就是代码工程
    if is_code_repo(project_dir):
        code_dirs.append(project_dir.name)

    # 2. 检查常见工程子目录（如 03_工程研发/*, apps/*, packages/*, impl-* 等）
    for sub in sorted(project_dir.iterdir()):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        if sub.name in ("03_工程研发", "apps", "packages", "source", "src", "site-ledger-admin", "site-ledger-app", "site-ledger-server") or sub.name.startswith("impl-"):
            if is_code_repo(sub):
                code_dirs.append(f"{project_dir.name}/{sub.name}")
            else:
                # 检查下一层
                for sub_child in sorted(sub.iterdir()):
                    if sub_child.is_dir() and not sub_child.name.startswith(".") and is_code_repo(sub_child):
                        code_dirs.append(f"{project_dir.name}/{sub.name}/{sub_child.name}")

    # 去重并保持顺序
    seen = set()
    result = []
    for c in code_dirs:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def is_knowledge_base(path: Path) -> tuple[bool, str]:
    """判断是否为知识库/共享资料目录，返回 (is_kb, reason)。"""
    lower_name = path.name.lower()
    if any(k in lower_name for k in KB_KEYWORDS):
        return True, f"目录名包含知识库关键词 '{path.name}'"

    # 检查内容构成：如大量 pdf/doc/文献/图谱
    pdf_count = len(list(path.glob("*.pdf"))) + len(list(path.glob("*/*.pdf")))
    doc_count = len(list(path.glob("*.doc*"))) + len(list(path.glob("*/*.doc*")))
    if pdf_count >= 3 or (pdf_count + doc_count) >= 3:
        return True, f"包含大量文献文档（{pdf_count} 个 PDF / {doc_count} 个 DOC）"

    return False, ""


def should_exclude(path: Path) -> tuple[bool, str]:
    """判断是否应直接加入 exclude（如安装包、临时工具、归档等）。"""
    lower_name = path.name.lower()
    if any(k in lower_name for k in EXCLUDE_KEYWORDS):
        return True, f"符合忽略/归档特征 '{path.name}'"

    # 包含安装包文件（.zip, .msi, .exe, .dmg）且非代码工程
    has_installers = any(path.glob("*.msi")) or any(path.glob("*.exe")) or any(path.glob("*.dmg"))
    if has_installers and not (path / ".git").exists() and not (path / "Cargo.toml").exists():
        return True, "包含系统安装包/分发文件"

    return False, ""


def scan_workspace(root: Path) -> dict[str, list[dict]]:
    """扫描工作区，输出分类结构：projects, shared_kbs, excludes。"""
    projects: list[dict] = []
    shared_kbs: list[dict] = []
    excludes: list[dict] = []

    # 已经存在的 exclude 与 projects
    existing_projects = {p["path"] for p in project_registry.load_projects(root)}
    existing_excludes = set(project_registry.load_excludes(root))

    for item in sorted(root.iterdir()):
        if not item.is_dir():
            continue
        name = item.name
        if name.startswith(".") or name == paths.SYSTEM_DIRNAME:
            continue

        item_path_str = f"{name}/"
        rel_path = name

        # 检查是否已有配置
        if rel_path in existing_projects or name in existing_projects:
            continue

        # 1. 检查是否为排除目录
        ex, reason = should_exclude(item)
        if ex:
            excludes.append({"name": name, "path": item_path_str, "reason": reason})
            continue

        # 2. 检查是否为知识库/共享资料目录
        kb, kb_reason = is_knowledge_base(item)
        if kb:
            shared_kbs.append({"name": name, "dir": item_path_str, "purpose": kb_reason})
            continue

        # 3. 检查是否为项目/工程
        code_subdirs = discover_sub_code(item)
        project_id = re.sub(r"[^a-zA-Z0-9_\-]+", "-", name.lower()).strip("-")
        projects.append({
            "id": project_id or name,
            "name": name,
            "path": rel_path,
            "code": code_subdirs if code_subdirs else ([rel_path] if is_code_repo(item) else []),
            "aliases": [name],
            "note": "自动扫描登记",
        })

    return {
        "projects": projects,
        "shared_kbs": shared_kbs,
        "excludes": excludes,
    }


def register_scanned(root: Path, scanned: dict[str, list[dict]], apply: bool = True) -> bool:
    """将扫描结果写入 registry.yaml 与 workspace-config.yaml。"""
    if not apply:
        return False

    yaml = _yaml()
    reg_file = project_registry.registry_path(root)
    ws_config_file = root / paths.SYSTEM_DIRNAME / "data" / "templates" / "workspace-config.yaml"

    # 1. 更新 workspace-config.yaml 中的 shared_dirs
    if scanned.get("shared_kbs") and ws_config_file.is_file():
        try:
            ws_data = yaml.safe_load(ws_config_file.read_text(encoding="utf-8")) or {}
            current_shared = ws_data.get("shared_dirs") or []
            existing_dirs = {s.get("dir") for s in current_shared if isinstance(s, dict)}
            added_shared = False
            for kb in scanned["shared_kbs"]:
                if kb["dir"] not in existing_dirs:
                    current_shared.append({"dir": kb["dir"], "purpose": kb["purpose"]})
                    added_shared = True
            if added_shared:
                ws_data["shared_dirs"] = current_shared
                ws_text = yaml.safe_dump(ws_data, allow_unicode=True, sort_keys=False)
                ws_config_file.write_text(ws_text, encoding="utf-8")
        except Exception as e:
            print(f"⚠️ 更新 workspace-config.yaml 失败: {e}", file=sys.stderr)

    # 2. 更新 registry.yaml 的 exclude 与 projects
    if reg_file.is_file():
        try:
            reg_text = reg_file.read_text(encoding="utf-8")
            reg_data = yaml.safe_load(reg_text) or {}

            # exclude: 合并扫描到的 excludes 以及 shared_kbs（shared_dirs 必须同时在 exclude 排除以防体检阻断）
            cur_excludes = list(reg_data.get("exclude") or [])
            cur_ex_set = set(cur_excludes)
            new_excludes = []
            for ex in scanned.get("excludes", []):
                p = ex["path"]
                if p not in cur_ex_set and p.rstrip("/") not in cur_ex_set:
                    new_excludes.append(p)
                    cur_ex_set.add(p)
            for kb in scanned.get("shared_kbs", []):
                p = kb["dir"]
                if p not in cur_ex_set and p.rstrip("/") not in cur_ex_set:
                    new_excludes.append(p)
                    cur_ex_set.add(p)

            if new_excludes:
                reg_data["exclude"] = cur_excludes + new_excludes

            # projects: 逐项追加
            existing_proj_ids = {p.get("id") for p in (reg_data.get("projects") or []) if isinstance(p, dict)}
            cur_projects = list(reg_data.get("projects") or [])
            for proj in scanned.get("projects", []):
                if proj["id"] not in existing_proj_ids:
                    # 保证结构合规
                    clean_entry = {
                        "id": proj["id"],
                        "name": proj["name"],
                        "path": f"{proj['path']}/",
                        "aliases": proj.get("aliases", []),
                    }
                    if proj.get("code"):
                        clean_entry["code"] = proj["code"]
                    if proj.get("note"):
                        clean_entry["note"] = proj["note"]
                    cur_projects.append(clean_entry)
                    existing_proj_ids.add(proj["id"])

            reg_data["projects"] = cur_projects

            # 重新写入 registry.yaml
            new_reg_text = yaml.safe_dump(reg_data, allow_unicode=True, sort_keys=False)
            reg_file.write_text(new_reg_text, encoding="utf-8")
            return True
        except Exception as e:
            print(f"⚠️ 更新 registry.yaml 失败: {e}", file=sys.stderr)
            return False

    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="自动扫描工作区并注册项目、工程与知识库")
    parser.add_argument("--root", type=Path, default=paths.WORKSPACE_ROOT, help="工作区根目录")
    parser.add_argument("--dry-run", action="store_true", help="仅显示扫描结果，不写入配置文件")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出扫描结果")

    args = parser.parse_args(argv)
    root = args.root.resolve()

    scanned = scan_workspace(root)

    if args.json:
        import json
        print(json.dumps(scanned, ensure_ascii=False, indent=2))
        return 0

    print(f"🔍 工作区扫描完成: {root}")
    print(f"📦 发现项目/工程 ({len(scanned['projects'])} 个):")
    for p in scanned["projects"]:
        code_str = f" [代码: {', '.join(p['code'])}]" if p.get("code") else ""
        print(f"  • {p['name']} ({p['id']}){code_str}")

    print(f"📚 发现知识库/共享资料 ({len(scanned['shared_kbs'])} 个):")
    for k in scanned["shared_kbs"]:
        print(f"  • {k['dir']} - {k['purpose']}")

    print(f"🚫 发现排除/归档目录 ({len(scanned['excludes'])} 个):")
    for e in scanned["excludes"]:
        print(f"  • {e['path']} - {e['reason']}")

    if not args.dry_run:
        registered = register_scanned(root, scanned, apply=True)
        if registered:
            print("✅ 扫描结果已自动写入 registry.yaml 与 workspace-config.yaml。")
        else:
            print("❌ 写入配置失败。")
            return 1
    else:
        print("ℹ️ Dry-run 模式：未修改配置文件。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
