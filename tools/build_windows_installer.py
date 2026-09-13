#!/usr/bin/env python3
"""构建 Windows 自解压安装包：把控制面封装成一个双击即用的 `.bat`。

产物是**单个文件**：批处理头部（找 Python → 自解压 → 调起安装程序）+ 尾部 Base64 载荷。
收件方双击它，弹框选安装目录，装完自动初始化，全程无需 git、无需 GitHub 账号、无需联网。

载荷一律取自 `git archive`（版本库跟踪集），而非文件系统上的 `.system/` 目录——私有 Skill
靠自带 `.gitignore` 排除出版本库但物理仍在，直接打包目录会把内部端点连同业务口径一起发出去。
《README》分发信道条款因此在这里由构建方式本身兑现，不依赖构建者记得绕开哪些目录。

遵循 ApX 工具契约：纯标准库、行动导向错误、结构化输出、临时目录原子组装后再替换。
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYSTEM_DIR = HERE.parent
WORKSPACE = SYSTEM_DIR.parent

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import install_windows  # noqa: E402 - 需先把同级 tools/ 挂上 sys.path

# 载荷标记与随包文件名都由安装侧定义，构建侧引用而非另写一份——两处各写一份的那天，
# 构建出的安装包就会在收件方机器上找不到自己的载荷。
PAYLOAD_MARKER = install_windows.PAYLOAD_MARKER.decode("ascii")
PAYLOAD_NAME = install_windows.PAYLOAD_NAME
DEFAULT_OUTPUT_NAME = "Entropaxis安装程序.bat"
GIT_TIMEOUT = 120
B64_LINE_WIDTH = 76

# 批处理头部。只做三件事：定位 Python、自解压到临时目录、把交互交给 install_windows.py。
# 刻意不用 PowerShell——收件方环境中 PowerShell 返回空输出或报错是已登记的已知问题。
BATCH_HEADER = """@echo off
chcp 65001 >nul
setlocal
title Entropaxis 安装程序
rem ---------------------------------------------------------------
rem Entropaxis 自解压安装包（由 .system/tools/build_windows_installer.py 生成）
rem 载荷为版本库跟踪集，以 Base64 附在本文件尾部的分隔标记之后。
rem ---------------------------------------------------------------

echo ========================================================
echo   Entropaxis 安装程序
echo ========================================================
echo.

rem Windows 上 python3 通常不在 PATH，优先用官方启动器 py -3
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python3 >nul 2>nul && set "PY=python3"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [X] 未检测到 Python 3。请先从 https://www.python.org/downloads/ 安装
    echo     并勾选 "Add python.exe to PATH"，然后重新双击本文件。
    echo.
    pause
    exit /b 1
)

set "STAGE=%TEMP%\\entropaxis-setup-%RANDOM%"
%PY% -c "import base64,io,sys,zipfile;raw=open(sys.argv[1],'rb').read().rsplit(b'{marker}',1)[1];zipfile.ZipFile(io.BytesIO(base64.b64decode(raw))).extractall(sys.argv[2])" "%~f0" "%STAGE%"
if errorlevel 1 (
    echo [X] 安装包自解压失败，文件可能在传输中损坏，请重新获取一份。
    pause
    exit /b 1
)

%PY% "%STAGE%\\tools\\install_windows.py" --carrier "%~f0"
set "RC=%ERRORLEVEL%"
rmdir /s /q "%STAGE%" 2>nul
echo.
pause
exit /b %RC%

{marker}
"""


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
            f"❌ {ref} 的跟踪集为空，构建出的安装包不含任何文件。\n"
            f"👉 修复建议: 先提交控制面内容再构建；未跟踪的文件不会被 archive 带走。"
        )
    return proc.stdout


def uncommitted_files(system: Path) -> list[str]:
    """列出未提交改动：它们不会进入安装包，构建前必须让人知道。"""
    try:
        proc = _git(system, "status", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]


def render_installer(payload: bytes) -> bytes:
    """拼装载体文件字节：批处理头部（CRLF）+ 折行 Base64 载荷。"""
    header = BATCH_HEADER.format(marker=PAYLOAD_MARKER).replace("\n", "\r\n")
    encoded = base64.b64encode(payload).decode("ascii")
    lines = [encoded[i : i + B64_LINE_WIDTH] for i in range(0, len(encoded), B64_LINE_WIDTH)]
    # 不写 BOM：cmd 会把 UTF-8 BOM 当成第一条命令的一部分，首行直接报"不是内部或外部命令"
    return header.encode("utf-8") + "\r\n".join(lines).encode("ascii") + b"\r\n"


def build_installer(
    system: Path = SYSTEM_DIR,
    out_path: Path | None = None,
    *,
    ref: str = "HEAD",
) -> dict:
    """构建安装包，返回结构化结果字典。"""
    system = Path(system).expanduser().resolve()
    if not (system / "tools" / "install_windows.py").is_file():
        raise ToolError(
            f"❌ 未找到安装程序本体: {system}/tools/install_windows.py\n"
            f"👉 修复建议: 请在完整的控制面仓库中运行本工具。"
        )

    target = Path(out_path).expanduser().resolve() if out_path else WORKSPACE / DEFAULT_OUTPUT_NAME
    payload = tracked_payload(system, ref)
    carrier = render_installer(payload)

    with tempfile.TemporaryDirectory(prefix="entropaxis-build-") as tmpdir:
        staged = Path(tmpdir) / target.name
        staged.write_bytes(carrier)
        # 回读自检：用安装侧的解析逻辑验一遍，杜绝"构建成功但装不上"
        extracted = install_windows.read_payload(staged)
        if extracted != payload:
            raise ToolError(
                "❌ 构建产物回读自检失败：取出的载荷与源载荷不一致。\n"
                "👉 修复建议: 请汇报至根系统治理流程，勿分发本次产物。"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        staged.replace(target)

    return {
        "status": "success",
        "installer": str(target),
        "ref": ref,
        "payload_bytes": len(payload),
        "installer_bytes": len(carrier),
        "uncommitted": uncommitted_files(system),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="构建 Windows 自解压安装包（单文件 .bat，载荷取自版本库跟踪集）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--out", help=f"产物路径，默认 <工作区根>/{DEFAULT_OUTPUT_NAME}")
    parser.add_argument("--ref", default="HEAD", help="打包的提交或分支，默认 HEAD")
    parser.add_argument(
        "--payload-only",
        action="store_true",
        help="只导出控制面载荷 ZIP（供 exe 构建流水线随包），不生成 .bat 载体",
    )
    parser.add_argument("--json", action="store_true", help="以结构化 JSON 格式输出结果")

    args = parser.parse_args()

    if args.payload_only:
        try:
            payload = tracked_payload(SYSTEM_DIR, args.ref)
        except ToolError as err:
            print(str(err), file=sys.stderr)
            return 1
        out = Path(args.out).expanduser().resolve() if args.out else WORKSPACE / PAYLOAD_NAME
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(payload)
        print(f"载荷: {out}（{len(payload) / 1024:.1f} KB，ref={args.ref}）")
        return 0

    try:
        res = build_installer(out_path=args.out, ref=args.ref)
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
        print(f"安装包: {res['installer']}")
        print(f"打包版本: {res['ref']}")
        print(f"载荷: {res['payload_bytes'] / 1024:.1f} KB / 安装包: {res['installer_bytes'] / 1024:.1f} KB")
        if res["uncommitted"]:
            print(f"⚠️ 有 {len(res['uncommitted'])} 项未提交改动未进入安装包：")
            for line in res["uncommitted"]:
                print(f"   • {line}")
        print("结论: ✅ 构建完成，可直接把该文件交给 Windows 收件方双击安装")
    return 0


if __name__ == "__main__":
    sys.exit(main())
