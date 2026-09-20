#!/usr/bin/env python3
"""按主题词定位事务胶囊，跨平台自动选用最快的检索后端。

为什么不自建索引：实测本工作区 11.5K 目录下 `fd -H -I` 全扫 0.6s 且 90/90 与 `find`
基准完全一致，比查系统索引还快——自建索引只会更慢、会漂移、还要维护。因此本工具
**不落盘任何索引或缓存**，只做后端分派与结果收敛。

新鲜度由检索顺序结构性保证：刚建的胶囊必然在当前工作区，而工作区范围走实时遍历
（fd/find，无索引）；机器范围才查系统索引，那些目标天然是旧的，索引滞后无所谓。
所以既不需要缓存失效机制，也不需要 init_capsule.py 回写索引。
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from . import paths
except ImportError:
    import paths

CAPSULE_RE = re.compile(r"^\d{8}_")

# 受审对象定位（《治理指令》审计 §2 / 修正 §2）：审计与修正都要先找"最新出现的方案或实施规格"。
# 形态真源见《文件交付》2.1（胶囊内 `序数_类型.md`）与 2.3（独立 Spec `<ID>_中文主题.md`、
# 配套审计报告 `Audit_<ID>_*.md`）。
# ponytail: 只按文件名形态 + mtime 排序返回候选，不猜"哪个才是本次受审对象"——多候选时
# 由调用方按规则向用户确认，工具不代做主观裁定。
ARTIFACT_PATTERNS = {
    "proposal": ("02_方案.md", "*_方案.md"),
    "spec": ("04_Spec_*.md", "*-[0-9]*_*.md"),
    "audit": ("05_审计报告.md", "Audit_*.md"),
}
_ARTIFACT_SKIP = {".git", "node_modules", "__pycache__", ".pytest_cache", "Archive"}

# 缺失时的安装指引。按平台给一条可直接粘贴的命令。
# ponytail: 只报不装——跨 brew/apt/dnf/pacman/winget/scoop 写自动安装要处理 sudo 交互与
# 各自的失败模式，代价远超收益；确有需要再加 --install 走非交互包管理器。
INSTALL_HINTS = {
    "Darwin": {"fd": "brew install fd", "rg": "brew install ripgrep"},
    "Linux": {"fd": "sudo apt install fd-find  # 或 dnf/pacman install fd", "rg": "sudo apt install ripgrep"},
    "Windows": {"fd": "winget install sharkdp.fd", "rg": "winget install BurntSushi.ripgrep.MSVC"},
}


class CapsuleFindError(Exception):
    """检索无法完成。"""


def _fd_binary() -> str | None:
    """Debian/Ubuntu 的包把可执行文件装成 fdfind，探测必须同时覆盖两个名字。"""
    for candidate in ("fd", "fdfind"):
        if shutil.which(candidate):
            return candidate
    return None


def _run(argv: list[str], timeout: int) -> list[str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line for line in done.stdout.splitlines() if line.strip()]


def search_workspace(query: str, root: Path, timeout: int) -> tuple[list[str], str, str | None]:
    """工作区范围：实时遍历，永不读索引。返回 (命中, 后端, 降级原因)。"""
    fd = _fd_binary()
    if fd:
        # -H 含隐藏目录、-I 忽略 .gitignore：两者缺一都会静默少结果——.entropaxis/data/
        # 正好被 gitignore 排除，实测不加 -I 会从 90 个胶囊掉到 70 个。
        return _run([fd, "-H", "-I", "-t", "d", query, str(root)], timeout), fd, None
    hint = INSTALL_HINTS.get(platform.system(), {}).get("fd", "安装 fd 可显著提速")
    hits = _run(["find", str(root), "-type", "d", "-name", f"*{query}*"], timeout)
    return hits, "find", f"未找到 fd，已降级为 find（更慢）；装上更快：{hint}"


def search_machine(query: str, scope_root: Path, timeout: int) -> tuple[list[str], str, str | None]:
    """机器范围：优先查系统索引，各平台后端不同，全缺时降级为实时遍历。"""
    system = platform.system()
    if system == "Darwin" and shutil.which("mdfind"):
        expr = f"kMDItemFSName == '*{query}*'c && kMDItemContentType == 'public.folder'"
        hits = _run(["mdfind", "-onlyin", str(scope_root), expr], timeout)
        # Spotlight 不索引隐藏目录，实测漏掉 .entropaxis/ 下全部胶囊；这是后端固有盲区，
        # 只能如实声明，不能假装覆盖完整。
        return hits, "mdfind", "Spotlight 不索引隐藏目录，`.` 开头路径下的胶囊不会出现在结果里"
    if system == "Windows" and shutil.which("es"):
        return _run(["es", "-p", "/ad", query], timeout), "es", None
    if system == "Linux":
        for locate in ("plocate", "locate"):
            if shutil.which(locate):
                hits = [h for h in _run([locate, "-i", query], timeout) if Path(h).is_dir()]
                return hits, locate, f"{locate} 数据库由 updatedb 定期刷新，最近新建的目录可能查不到"
    hits, backend, reason = search_workspace(query, scope_root, timeout)
    return hits, backend, f"本平台无可用系统索引，已降级为实时遍历 {scope_root}" + (f"；{reason}" if reason else "")


def load_registry(root: Path) -> list[dict]:
    """读项目映射表，取「主目录」与「口语别名」两列用于范围收敛与排序加权。

    层级不从主目录路径推断——`父项目` 列才是真源，子项目可嵌套也可平铺。
    """
    registry = root / paths.SYSTEM_DIRNAME / "data" / "templates" / "registry.md"
    if not registry.is_file():
        return []
    projects = []
    table, _, _ = registry.read_text(encoding="utf-8").partition("## 排除规则")
    for line in table.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("| 项目 ID") or line.startswith("|-"):
            continue
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if len(cols) < 5:
            continue
        dirs = re.findall(r"`([^`]+)`", cols[2])
        if not dirs or "待填写" in cols[0]:
            continue
        projects.append({
            "id": cols[0],
            "dir": dirs[0].rstrip("/"),
            "parent": cols[3],
            "aliases": [a.strip() for a in cols[4].split(",") if a.strip()],
        })
    return projects


def resolve_scope(query: str, projects: list[dict]) -> dict | None:
    """别名命中则把检索范围收敛到该项目主目录，这是省时省 token 的最大杠杆。"""
    lowered = query.lower()
    for project in projects:
        if any(lowered == alias.lower() for alias in project["aliases"]):
            return project
    for project in projects:
        if any(alias.lower() in lowered or lowered in alias.lower() for alias in project["aliases"]):
            return project
    return None


def find_capsule(query: str, root: Path, scope: str = "workspace",
                 capsules_only: bool = True, timeout: int = 60) -> dict:
    query = query.strip()
    if not query:
        raise CapsuleFindError("检索词为空")

    projects = load_registry(root)
    matched = resolve_scope(query, projects) if scope == "workspace" else None
    search_root = root / matched["dir"] if matched and (root / matched["dir"]).is_dir() else root

    if scope == "machine":
        hits, backend, degraded = search_machine(query, search_root, timeout)
    else:
        hits, backend, degraded = search_workspace(query, search_root, timeout)

    results = []
    for hit in hits:
        path = Path(hit.rstrip("/"))
        if capsules_only and not CAPSULE_RE.match(path.name):
            continue
        try:
            rel = str(path.resolve().relative_to(root.resolve()))
        except ValueError:
            rel = str(path)
        results.append(rel)
    # 新胶囊在前：日期在**目录名**开头，所以按 basename 倒排；按整条路径排会先比到
    # 父目录名，日期就不起作用了。
    results = sorted(dict.fromkeys(results), key=lambda p: (Path(p).name, p), reverse=True)

    return {
        "query": query,
        "scope": scope,
        "backend": backend,
        "search_root": str(search_root),
        "matched_project": matched["id"] if matched else None,
        "degraded": degraded,
        "count": len(results),
        "results": results,
    }


def find_artifacts(kind: str, root: Path, query: str | None = None,
                   limit: int = 20, timeout: int = 60) -> dict:
    """定位最新的方案 / Spec / 审计报告候选，按修改时间倒排。

    query 给出时先用既有胶囊检索把范围收敛到命中的胶囊目录，命中不到才退回整个
    search_root——收敛是省 token 的主要来源，全库 rglob 是兜底不是主路径。
    """
    if kind not in ARTIFACT_PATTERNS:
        raise CapsuleFindError(f"未知交付物类型 {kind!r}，可选：{'/'.join(ARTIFACT_PATTERNS)}")

    scopes: list[Path] = []
    matched_project = None
    if query:
        report = find_capsule(query, root, scope="workspace", capsules_only=True, timeout=timeout)
        matched_project = report["matched_project"]
        scopes = [root / r for r in report["results"]]
    if not scopes:
        scopes = [root]

    seen: dict[Path, float] = {}
    for scope in scopes:
        if not scope.is_dir():
            continue
        for pattern in ARTIFACT_PATTERNS[kind]:
            for hit in scope.rglob(pattern):
                if not hit.is_file() or _ARTIFACT_SKIP & set(hit.parts):
                    continue
                seen[hit.resolve()] = hit.stat().st_mtime

    ordered = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)
    results = []
    for path, mtime in ordered[:limit]:
        try:
            rel = str(path.relative_to(root.resolve()))
        except ValueError:
            rel = str(path)
        results.append({"path": rel, "mtime": int(mtime)})

    return {
        "kind": kind,
        "query": query,
        "matched_project": matched_project,
        "scopes": [str(s) for s in scopes],
        "count": len(ordered),
        "truncated": len(ordered) > limit,
        "results": results,
    }


def doctor() -> dict:
    """报告本机可用后端与缺失项的安装命令，不执行任何安装。"""
    system = platform.system()
    hints = INSTALL_HINTS.get(system, {})
    available = {
        "fd": _fd_binary(),
        "rg": shutil.which("rg"),
        "system_index": next(
            (b for b in ("mdfind", "es", "plocate", "locate") if shutil.which(b)), None
        ),
    }
    missing = {name: hints[name] for name in ("fd", "rg") if not available[name] and name in hints}
    return {"platform": system, "available": available, "install_hints": missing}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按主题词定位事务胶囊（跨平台后端自动分派，不落盘索引）")
    parser.add_argument("query", nargs="?", help="主题词或项目别名")
    parser.add_argument("--scope", choices=("workspace", "machine"), default="workspace",
                        help="workspace=实时遍历当前工作区（默认）；machine=查系统索引")
    parser.add_argument("--root", type=Path, default=paths.WORKSPACE_ROOT, help="工作区根，默认自动推导")
    parser.add_argument("--any", action="store_true", help="不限于胶囊目录，返回全部命中目录")
    parser.add_argument("--timeout", type=int, default=60, help="单次检索超时秒数")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--doctor", action="store_true", help="只报告可用检索后端与安装建议")
    parser.add_argument("--latest-artifact", choices=tuple(ARTIFACT_PATTERNS),
                        help="定位最新的方案/Spec/审计报告候选（审计与修正的受审对象定位）")
    parser.add_argument("--limit", type=int, default=20, help="交付物候选返回上限，默认 20")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()

    if args.doctor:
        print(json.dumps(doctor(), ensure_ascii=False, indent=2))
        return

    if args.latest_artifact:
        try:
            report = find_artifacts(args.latest_artifact, args.root.expanduser().resolve(),
                                    query=args.query, limit=args.limit, timeout=args.timeout)
        except CapsuleFindError as error:
            print(f"❌ {error}\n👉 用法：find_capsule.py --latest-artifact audit [主题词]", file=sys.stderr)
            raise SystemExit(2)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        if not report["results"]:
            print(f"❌ 未找到 {args.latest_artifact} 类交付物（范围 {report['scopes'][0]}）", file=sys.stderr)
            print("👉 换主题词收敛范围，或确认该胶囊内确已产出该阶段文档", file=sys.stderr)
            raise SystemExit(1)
        for item in report["results"]:
            print(item["path"])
        if report["truncated"]:
            print(f"… 另有 {report['count'] - len(report['results'])} 条，用 --limit 放宽", file=sys.stderr)
        return

    if not args.query:
        parser.error("缺少检索词\n👉 用法：find_capsule.py <主题词> [--scope machine]")

    try:
        report = find_capsule(args.query, args.root.expanduser().resolve(),
                              scope=args.scope, capsules_only=not args.any, timeout=args.timeout)
    except CapsuleFindError as error:
        print(f"❌ {error}\n👉 换一个主题词，或加 --any 放宽到全部目录", file=sys.stderr)
        raise SystemExit(2)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if report["degraded"]:
        print(f"⚠️  {report['degraded']}", file=sys.stderr)
    if not report["results"]:
        print(f"❌ 未命中「{report['query']}」（后端 {report['backend']}，范围 {report['search_root']}）", file=sys.stderr)
        print("👉 换更短的主题词，或用 --scope machine 扩到全机器，或加 --any 放宽到非胶囊目录", file=sys.stderr)
        raise SystemExit(1)
    for item in report["results"]:
        print(item)


if __name__ == "__main__":
    main()
