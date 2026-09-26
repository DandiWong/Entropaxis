#!/usr/bin/env python3
"""本地发版：隔离合并 → 验证 → 主干与附注 tag 单事务更新（不推送）。

真源定义: rules/软件工程.md「发版与迭代边界」的「发版 vX.Y.Z」。

存在的理由（《工具设计》信号 3 精确计算 + 信号 4 高危动作）:
    发版要在隔离环境合并并验证、预检 tag、再让主干指针与 tag 同批次原子更新，
    失败时主干一步不动。手写 git 序列极易出现"tag 打上了主干没动"或"主干动了验证没跑"
    的半成品，且 update-ref 事务语法、附注 tag 对象构造都是字符级精确动作。

设计不变量:
  1. **隔离**：合并与验证只在临时 worktree 里发生，不碰任何现有工作区。
  2. **全绿才写**：验证命令退出码非 0 即中止，主干与 tag 均不变，验证日志保留在临时目录。
  3. **单事务**：主干（带旧值校验）与新 tag 在一次 `git update-ref --stdin` 中更新，要么都成要么都不动；
     主干在此期间被他人移动时旧值校验失败，整体放弃。
  4. **不覆盖、不推送**：同名 tag 已存在即拒绝；版本号须高于已有最大语义化版本；从不执行 push。
  5. **检出态同步**：主干若被某个工作区检出，要求其干净，事务成功后以两树合并快进其索引与文件。
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class ToolError(Exception):
    """可恢复业务异常，包含行动导向修复指引。"""


def _git(repo: Path, *args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *args], input=input_text,
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise ToolError(
            f"❌ git {' '.join(args)} 失败: {proc.stderr.strip() or proc.stdout.strip()}\n"
            "👉 修复建议: 按 git 报错修正仓库状态后重试；主干与 tag 未被修改。"
        )
    return proc


def _rev(repo: Path, ref: str) -> str | None:
    proc = _git(repo, "rev-parse", "-q", "--verify", ref, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _semver(tag: str) -> tuple[int, int, int] | None:
    m = VERSION_RE.match(tag)
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


def _checked_out_path(repo: Path, branch_ref: str) -> Path | None:
    """返回检出该分支的工作区路径；未检出返回 None。"""
    current = None
    for line in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):])
        elif line == f"branch {branch_ref}" and current is not None:
            return current
    return None


def plan_release(repo: Path, version: str, source: str | None, main: str | None) -> dict:
    """只读预检，返回发版计划；任一前置条件不满足即抛 ToolError。"""
    if not VERSION_RE.match(version):
        raise ToolError(f"❌ 版本号 {version!r} 不是 vX.Y.Z 形式。\n👉 修复建议: 例如 v1.4.0。")
    top = _git(repo, "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0:
        raise ToolError(f"❌ {repo} 不是 git 仓库。\n👉 修复建议: 用 --repo 指向项目仓库根。")
    repo = Path(top.stdout.strip())

    if main is None:
        main = next((b for b in ("main", "master") if _rev(repo, f"refs/heads/{b}")), None)
        if main is None:
            raise ToolError("❌ 未找到 main 或 master 分支。\n👉 修复建议: 用 --main 指定主干分支名。")
    main_sha = _rev(repo, f"refs/heads/{main}")
    if not main_sha:
        raise ToolError(f"❌ 主干分支 {main!r} 不存在。\n👉 修复建议: 用 --main 指定正确的主干分支名。")

    if source is None:
        head = _git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
        source = head or main
    source_sha = _rev(repo, f"{source}^{{commit}}")
    if not source_sha:
        raise ToolError(f"❌ 待发分支 {source!r} 不存在。\n👉 修复建议: 用 --source 指定已验证的功能分支。")

    if _rev(repo, f"refs/tags/{version}"):
        raise ToolError(f"❌ tag {version} 已存在，发版不覆盖既有 tag。\n👉 修复建议: 换一个更高的版本号。")
    tags = [t for t in _git(repo, "tag", "--list", "v*").stdout.split() if _semver(t)]
    latest = max(tags, key=_semver, default=None)
    if latest and _semver(version) <= _semver(latest):  # type: ignore[operator]
        raise ToolError(f"❌ 版本 {version} 不高于已有最大版本 {latest}。\n👉 修复建议: 选用高于 {latest} 的版本号。")

    pending = _git(repo, "log", "--format=%h %s", f"{main_sha}..{source_sha}").stdout.splitlines()
    checked_out = _checked_out_path(repo, f"refs/heads/{main}")
    if checked_out and _git(checked_out, "status", "--porcelain").stdout.strip():
        raise ToolError(
            f"❌ 主干 {main} 检出于 {checked_out}，该工作区有未提交改动。\n"
            "👉 修复建议: 先提交或暂存该工作区改动，再发版；本工具不会替你处理未提交内容。"
        )
    return {"repo": repo, "version": version, "main": main, "main_sha": main_sha, "source": source,
            "source_sha": source_sha, "pending": pending, "latest_tag": latest, "checked_out": checked_out}


def cut_release(plan: dict, verify: str, message: str | None, timeout: int) -> dict:
    repo, main, version = plan["repo"], plan["main"], plan["version"]
    tmp = Path(tempfile.mkdtemp(prefix="cut-release-"))
    wt, log = tmp / "wt", tmp / "verify.log"
    ok = False
    try:
        _git(repo, "worktree", "add", "--detach", str(wt), plan["main_sha"])
        if plan["pending"]:
            merge = _git(wt, "merge", "--no-edit", "-m", f"Merge {plan['source']} for {version}",
                         plan["source_sha"], check=False)
            if merge.returncode != 0:
                _git(wt, "merge", "--abort", check=False)
                raise ToolError(
                    f"❌ {plan['source']} 合入 {main} 有冲突: {merge.stdout.strip()[-300:]}\n"
                    "👉 修复建议: 在功能分支上先合并主干并解决冲突、验证通过后再发版。"
                )
        try:
            proc = subprocess.run(shlex.split(verify), cwd=wt, capture_output=True, text=True, timeout=timeout)
            code, output = proc.returncode, proc.stdout + proc.stderr
        except (OSError, subprocess.TimeoutExpired) as err:
            code, output = -1, f"{type(err).__name__}: {err}"
        log.write_text(output, encoding="utf-8")
        if code != 0:
            raise ToolError(
                f"❌ 验证命令未通过（退出码 {code}），主干与 tag 未改动。日志: {log}\n"
                "👉 修复建议: 按日志修复后在功能分支提交，再重新发版。"
            )

        new_sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
        ident = _git(repo, "var", "GIT_COMMITTER_IDENT").stdout.strip()
        body = message or f"Release {version}"
        tag_sha = _git(repo, "mktag", input_text=(
            f"object {new_sha}\ntype commit\ntag {version}\ntagger {ident}\n\n{body}\n")).stdout.strip()
        _git(repo, "update-ref", "--stdin", input_text=(
            f"update refs/heads/{main} {new_sha} {plan['main_sha']}\n"
            f"create refs/tags/{version} {tag_sha}\n"))
        ok = True

        synced, warning = None, None
        if plan["checked_out"] and new_sha != plan["main_sha"]:
            sync = _git(plan["checked_out"], "read-tree", "-m", "-u", plan["main_sha"], new_sha, check=False)
            synced = str(plan["checked_out"]) if sync.returncode == 0 else None
            if sync.returncode != 0:
                warning = (f"主干与 tag 已更新，但 {plan['checked_out']} 的文件未同步：{sync.stderr.strip()}；"
                           f"该工作区发版前是干净的，可执行 git -C {plan['checked_out']} reset --hard HEAD 同步。")
        return {"ok": True, "version": version, "main": main, "commit": new_sha, "tag_object": tag_sha,
                "merged": len(plan["pending"]), "synced_worktree": synced, "warning": warning, "pushed": False}
    finally:
        if wt.exists():
            _git(repo, "worktree", "remove", "--force", str(wt), check=False)
        _git(repo, "worktree", "prune", check=False)
        if ok:
            shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="本地发版：隔离合并 → 验证 → 主干与附注 tag 单事务更新（不推送）")
    parser.add_argument("version", help="版本号，vX.Y.Z")
    parser.add_argument("--verify", required=True, help="全量验证命令，在隔离工作区执行，如 \"python3 -m unittest\"")
    parser.add_argument("--repo", default=".", help="仓库路径，默认当前目录")
    parser.add_argument("--source", help="待发分支，默认当前分支")
    parser.add_argument("--main", help="主干分支，默认自动识别 main/master")
    parser.add_argument("--message", help="tag 说明，默认 Release <version>")
    parser.add_argument("--timeout", type=int, default=1800, help="验证命令超时秒数，默认 1800")
    parser.add_argument("--dry-run", action="store_true", help="只预检并输出计划，不做任何写入")
    parser.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    args = parser.parse_args()

    try:
        plan = plan_release(Path(args.repo).expanduser(), args.version, args.source, args.main)
        if args.dry_run:
            result = {"ok": True, "dry_run": True, "version": plan["version"], "main": plan["main"],
                      "source": plan["source"], "pending": plan["pending"], "latest_tag": plan["latest_tag"],
                      "checked_out": str(plan["checked_out"]) if plan["checked_out"] else None}
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else
                  f"✅ 预检通过: {plan['source']} → {plan['main']}，待合入 {len(plan['pending'])} 个提交，拟打 {plan['version']}")
            return 0
        result = cut_release(plan, args.verify, args.message, args.timeout)
    except ToolError as err:
        print(str(err), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"✅ {result['version']} → {result['main']}@{result['commit'][:7]}（合入 {result['merged']} 个提交，验证通过，未推送）")
        if result["warning"]:
            print(f"⚠️ {result['warning']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
