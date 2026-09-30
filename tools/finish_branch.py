#!/usr/bin/env python3
"""收尾分支：提交已暂存改动 → 合入目标分支 → 推送远端 → 删除当前分支。

真源定义: rules/软件工程.md「发版与迭代边界」的「收尾分支」。

存在的理由（《工具设计》信号 4 高危动作）:
    推送与删分支不可逆。手写序列常见的半成品是"合并没推上去分支先删了"、
    "把没打算提交的改动一起带进了目标分支"、"本地目标分支落后远端，推送被拒后乱修"。

设计不变量:
  1. **只提交已暂存内容**：已跟踪文件有未暂存改动即拒绝（由调用方先精确 git add）；未跟踪文件不提交、只报告。
  2. **先同步再合并**：远端有目标分支时，本地目标分支须能快进到远端，分叉即中止。
  3. **冲突即回滚**：合并冲突时 merge --abort 并切回原分支，目标分支不动。
  4. **推送成功才删分支**：推送失败时原分支保留；删本地分支用 `branch -d`（未合入即拒绝）。
  5. **远端分支默认不删**：远端同名分支仅在 --delete-remote 时删除。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


class ToolError(Exception):
    """可恢复业务异常，包含行动导向修复指引。"""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise ToolError(
            f"❌ git {' '.join(args)} 失败: {proc.stderr.strip() or proc.stdout.strip()}\n"
            "👉 修复建议: 按 git 报错修正仓库状态后重试。"
        )
    return proc


def _rev(repo: Path, ref: str) -> str | None:
    proc = _git(repo, "rev-parse", "-q", "--verify", ref, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _repo_and_branch(repo: Path) -> tuple[Path, str]:
    top = _git(repo, "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0:
        raise ToolError(f"❌ {repo} 不是 git 仓库。\n👉 修复建议: 用 --repo 指向项目仓库根。")
    repo = Path(top.stdout.strip())
    branch = _git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    if not branch:
        raise ToolError("❌ 当前处于分离 HEAD，没有可收尾的分支。\n👉 修复建议: 先切到要收尾的功能分支。")
    return repo, branch


def list_targets(repo: Path, remote: str) -> dict:
    repo, source = _repo_and_branch(repo)
    local = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").stdout.split()
    remote_refs = _git(repo, "for-each-ref", "--format=%(refname:short)", f"refs/remotes/{remote}").stdout.split()
    names = set(local) | {r.split("/", 1)[1] for r in remote_refs if r.count("/") >= 1 and not r.endswith("/HEAD")}
    names.discard(source)
    trunk = [b for b in ("main", "master") if b in names]
    return {"source": source, "targets": trunk + sorted(names - set(trunk))}


def plan_finish(repo: Path, target: str, remote: str, message: str | None) -> dict:
    """只读预检；任一前置条件不满足即抛 ToolError。"""
    repo, source = _repo_and_branch(repo)
    if target == source:
        raise ToolError(f"❌ 目标分支与当前分支同为 {source!r}。\n👉 修复建议: 换一个目标分支，或先切到功能分支。")
    if _git(repo, "remote", "get-url", remote, check=False).returncode != 0:
        raise ToolError(f"❌ 远端 {remote!r} 不存在。\n👉 修复建议: 用 --remote 指定远端名（git remote -v 查看）。")

    status = _git(repo, "status", "--porcelain").stdout.splitlines()
    staged = [ln[3:] for ln in status if ln[0] not in " ?"]
    unstaged = [ln[3:] for ln in status if ln[0] != "?" and ln[1] != " "]
    untracked = [ln[3:] for ln in status if ln.startswith("??")]
    if unstaged:
        raise ToolError(
            f"❌ 已跟踪文件有未暂存改动: {', '.join(unstaged[:10])}\n"
            "👉 修复建议: 对本次要提交的文件 git add，其余 git stash 或还原后重试；本工具只提交已暂存内容。"
        )
    if staged and not message:
        raise ToolError("❌ 有已暂存改动但未给提交信息。\n👉 修复建议: 加 --message \"<提交信息>\"。")

    local_target = _rev(repo, f"refs/heads/{target}")
    remote_target = _rev(repo, f"refs/remotes/{remote}/{target}")
    if not local_target and not remote_target:
        raise ToolError(
            f"❌ 目标分支 {target!r} 在本地和 {remote} 上都不存在。\n"
            "👉 修复建议: 用 --list-targets 查看候选，或先 git fetch 再重试。"
        )
    for line in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line == f"branch refs/heads/{target}":
            raise ToolError(
                f"❌ 目标分支 {target} 已在另一个工作区检出。\n👉 修复建议: 在那个工作区切走 {target} 后重试。"
            )
    pending = _git(repo, "log", "--format=%h %s", f"{local_target or remote_target}..{source}").stdout.splitlines()
    return {"repo": repo, "source": source, "target": target, "remote": remote, "message": message,
            "staged": staged, "untracked": untracked, "pending": pending,
            "has_remote_source": bool(_rev(repo, f"refs/remotes/{remote}/{source}"))}


def finish_branch(plan: dict, delete_remote: bool) -> dict:
    repo, source, target, remote = plan["repo"], plan["source"], plan["target"], plan["remote"]
    committed = None
    if plan["staged"]:
        _git(repo, "commit", "-q", "-m", plan["message"])
        committed = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()

    fetched = _git(repo, "fetch", "-q", remote, target, check=False).returncode == 0
    remote_ref = f"refs/remotes/{remote}/{target}"
    if _rev(repo, f"refs/heads/{target}"):
        _git(repo, "checkout", "-q", target)
        if fetched and _rev(repo, remote_ref):
            ff = _git(repo, "merge", "-q", "--ff-only", remote_ref, check=False)
            if ff.returncode != 0:
                _git(repo, "checkout", "-q", source, check=False)
                raise ToolError(
                    f"❌ 本地 {target} 与 {remote}/{target} 已分叉，无法快进同步；未合并、未推送。\n"
                    f"👉 修复建议: 先人工对齐本地 {target} 与远端，再重试。"
                )
    else:
        _git(repo, "checkout", "-q", "-b", target, "--track", f"{remote}/{target}")

    merged_commits = int(_git(repo, "rev-list", "--count", f"HEAD..{source}").stdout.strip())
    merge = _git(repo, "merge", "--no-edit", source, check=False)
    if merge.returncode != 0:
        _git(repo, "merge", "--abort", check=False)
        _git(repo, "checkout", "-q", source, check=False)
        raise ToolError(
            f"❌ {source} 合入 {target} 有冲突，已中止合并并切回 {source}: {merge.stdout.strip()[-300:]}\n"
            f"👉 修复建议: 在 {source} 上先合并 {target} 并解决冲突、验证通过后再收尾。"
        )
    merged = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()

    push = _git(repo, "push", "-u", remote, target, check=False)
    if push.returncode != 0:
        raise ToolError(
            f"❌ 推送 {target} 到 {remote} 失败: {push.stderr.strip()[-300:]}\n"
            f"👉 修复建议: 合并已在本地 {target} 完成（当前停在 {target}），{source} 未删除；"
            "解决推送问题（权限、保护分支、远端已前进）后 git push，再删除分支。"
        )

    _git(repo, "branch", "-d", source)
    warning = None
    remote_deleted = False
    if delete_remote and plan["has_remote_source"]:
        rd = _git(repo, "push", remote, "--delete", source, check=False)
        remote_deleted = rd.returncode == 0
        if not remote_deleted:
            warning = f"远端分支 {remote}/{source} 删除失败: {rd.stderr.strip()[-200:]}"
    elif plan["has_remote_source"]:
        warning = f"远端分支 {remote}/{source} 仍保留（需要删除时加 --delete-remote）。"
    if plan["untracked"]:
        extra = f"未跟踪文件未提交: {', '.join(plan['untracked'][:10])}"
        warning = f"{warning}；{extra}" if warning else extra
    return {"ok": True, "source": source, "target": target, "remote": remote, "committed": committed,
            "merge_commit": merged, "merged_commits": merged_commits, "pushed": True,
            "local_deleted": True, "remote_deleted": remote_deleted, "warning": warning}


def main() -> int:
    parser = argparse.ArgumentParser(description="收尾分支：提交已暂存改动 → 合入目标分支 → 推送 → 删除当前分支")
    parser.add_argument("--target", help="目标分支；未指定时先用 --list-targets 取候选")
    parser.add_argument("--message", help="提交信息（有已暂存改动时必填）")
    parser.add_argument("--repo", default=".", help="仓库路径，默认当前目录")
    parser.add_argument("--remote", default="origin", help="远端名，默认 origin")
    parser.add_argument("--delete-remote", action="store_true", help="同时删除远端同名分支")
    parser.add_argument("--list-targets", action="store_true", help="只列出可选目标分支")
    parser.add_argument("--dry-run", action="store_true", help="只预检并输出计划，不做任何写入")
    parser.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    args = parser.parse_args()
    repo = Path(args.repo).expanduser()

    try:
        if args.list_targets:
            result = list_targets(repo, args.remote)
            print(json.dumps(result, ensure_ascii=False) if args.json else
                  f"当前 {result['source']}；可选目标: {', '.join(result['targets']) or '（无）'}")
            return 0
        if not args.target:
            raise ToolError("❌ 未指定目标分支。\n👉 修复建议: 先 --list-targets 取候选并由人选定，再加 --target。")
        plan = plan_finish(repo, args.target, args.remote, args.message)
        if args.dry_run:
            result = {"ok": True, "dry_run": True, "source": plan["source"], "target": plan["target"],
                      "staged": plan["staged"], "untracked": plan["untracked"], "pending": plan["pending"]}
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else
                  f"✅ 预检通过: {plan['source']} → {plan['target']}，待提交 {len(plan['staged'])} 个文件、"
                  f"待合入 {len(plan['pending'])} 个提交；未跟踪 {len(plan['untracked'])} 个不提交")
            return 0
        result = finish_branch(plan, args.delete_remote)
    except ToolError as err:
        print(str(err), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"✅ {result['source']} → {result['target']}@{result['merge_commit']} 已推送 {result['remote']}，"
              f"本地分支已删除{'（含远端）' if result['remote_deleted'] else ''}")
        if result["warning"]:
            print(f"⚠️ {result['warning']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
