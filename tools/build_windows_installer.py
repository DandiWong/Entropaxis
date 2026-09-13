#!/usr/bin/env python3
"""导出 Windows exe 安装包的随包载荷：版本库跟踪集打成一个 ZIP。

用于 `.github/workflows/build-windows-installer.yml`：该工作流在 `windows-latest` 上跑
本工具取到载荷，随后用 PyInstaller 把 `install_windows.py` 与这份载荷一起冻结成单文件
exe——exe 双击即装，全程不联网、不拉远端。本工具本身不做冻结（那一步需要 Windows）。

载荷一律取自 `git archive`（版本库跟踪集），而非文件系统上的 `.system/` 目录——私有 Skill
靠自带 `.gitignore` 排除出版本库但物理仍在，直接打包目录会把内部端点连同业务口径一起发出去。
《README》分发信道条款因此在这里由取数方式本身兑现，不依赖调用者记得绕开哪些目录。

遵循 ApX 工具契约：纯标准库、行动导向错误、结构化输出。
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYSTEM_DIR = HERE.parent
WORKSPACE = SYSTEM_DIR.parent

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import install_windows  # noqa: E402 - 需先把同级 tools/ 挂上 sys.path

# 随包文件名由安装侧定义，这里引用而非另写一份——两处各写一份的那天，
# 冻结出的 exe 就会在收件方机器上找不到自己的载荷。
PAYLOAD_NAME = install_windows.PAYLOAD_NAME
GIT_TIMEOUT = 120


class ToolError(Exception):
    """工具可恢复业务异常，包含行动导向修复指引。"""


def _git(system: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(system), *args],
        capture_output=True,
        timeout=GIT_TIMEOUT,
    )


def tracked_payload(system: Path, ref: str) -> bytes:
    """用 `git archive` 取版本库跟踪集，返回 ZIP 字节。"""
    try:
        proc = _git(system, "archive", "--format=zip", ref)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolError(
            f"❌ 无法执行 git: {exc}\n"
            f"👉 修复建议: 安装包载荷只能取自版本库跟踪集，请确认已安装 git 且 {system} 是一个仓库。"
        ) from exc
    if proc.returncode != 0:
        raise ToolError(
            f"❌ git archive 失败（退出码 {proc.returncode}，ref={ref}）：{proc.stderr.decode('utf-8', 'replace').strip()}\n"
            f"👉 修复建议: 确认 {ref} 是有效的提交或分支，且该分支已有提交内容。"
        )
    if not proc.stdout:
        raise ToolError(
            f"❌ {ref} 的跟踪集为空，导出的载荷不含任何文件。\n"
            f"👉 修复建议: 先提交控制面内容再构建；未跟踪的文件不会被 archive 带走。"
        )
    return proc.stdout


def uncommitted_files(system: Path) -> list[str]:
    """列出未提交改动：它们不会进入载荷，构建前必须让人知道。"""
    try:
        proc = _git(system, "status", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]


def export_payload(system: Path = SYSTEM_DIR, out_path: Path | None = None, *, ref: str = "HEAD") -> dict:
    """导出载荷 ZIP 到指定路径，返回结构化结果字典。"""
    system = Path(system).expanduser().resolve()
    payload = tracked_payload(system, ref)

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
    missing = [m for m in install_windows.REQUIRED_MEMBERS if m not in names]
    if missing:
        raise ToolError(
            f"❌ 导出的载荷不完整，缺少: {', '.join(missing)}\n"
            f"👉 修复建议: 确认 {ref} 上这些文件确已提交，而非只在工作区本地存在。"
        )

    target = Path(out_path).expanduser().resolve() if out_path else WORKSPACE / PAYLOAD_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)

    return {
        "status": "success",
        "payload": str(target),
        "ref": ref,
        "payload_bytes": len(payload),
        "uncommitted": uncommitted_files(system),
    }


def main() -> int:
    install_windows._ensure_utf8_console()
    parser = argparse.ArgumentParser(
        description="导出 Windows exe 安装包的随包载荷（版本库跟踪集 ZIP）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--out", help=f"产物路径，默认 <工作区根>/{PAYLOAD_NAME}")
    parser.add_argument("--ref", default="HEAD", help="打包的提交或分支，默认 HEAD")
    parser.add_argument("--json", action="store_true", help="以结构化 JSON 格式输出结果")

    args = parser.parse_args()

    try:
        res = export_payload(out_path=args.out, ref=args.ref)
    except ToolError as err:
        print(str(err), file=sys.stderr)
        return 1
    except Exception as err:  # noqa: BLE001
        print(
            f"❌ 意外系统异常: {err}\n👉 修复建议: 请检查环境权限或汇报至根系统治理流程。",
            file=sys.stderr,
        )
        return 2

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"载荷: {res['payload']}（{res['payload_bytes'] / 1024:.1f} KB，ref={res['ref']}）")
        if res["uncommitted"]:
            print(f"⚠️ 有 {len(res['uncommitted'])} 项未提交改动未进入载荷：")
            for line in res["uncommitted"]:
                print(f"   • {line}")
        print("结论: ✅ 载荷导出完成，交给 PyInstaller 随包冻结即可")
    return 0


if __name__ == "__main__":
    sys.exit(main())
