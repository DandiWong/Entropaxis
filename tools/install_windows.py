#!/usr/bin/env python3
"""Windows 安装程序本体：选目录 → 展开控制面 → 跑初始化。

由 `build_windows_installer.py` 生成的自解压 `.bat` 载体调用：载体把自身尾部的 Base64
载荷解出一份到临时目录，再执行本文件；本文件负责与人交互（图形化选安装目录）、把控制面
落到用户选定的目录，并接着跑 `bootstrap.py` 完成初始化。

两条安全边界（均为信任边界，不做简化）：
  1. **只写 `.system/`**：`.data/` 是用户实例数据与凭据所在，安装与升级全程不触碰；
  2. **载荷成员路径校验**：载荷虽由本仓库 `git archive` 产出，但经过传输与落盘后即为
     外部输入，解压前逐条排除绝对路径与 `..`（Zip Slip）。

遵循 ApX 工具契约：纯标准库、行动导向错误、结构化输出、临时目录原子组装后再落盘。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import io
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# 载体 `.bat` 尾部的载荷分隔标记。载体正文里还有一处同名字面量（自解压那行 `-c` 代码），
# 因此取值一律用 rsplit 取最后一段，避免切到那处诱饵。
PAYLOAD_MARKER = b"#ENTROPAXIS_PAYLOAD#"

# 载荷完整性判据：缺其一即说明拿到的不是一份完整控制面，宁可阻断也不落一半。
REQUIRED_MEMBERS = ("tools/bootstrap.py", "entrypoints/AGENTS.md", "entrypoints/CLAUDE.md")

BOOTSTRAP_TIMEOUT = 300


class ToolError(Exception):
    """工具可恢复业务异常，包含行动导向修复指引。"""


def read_payload(carrier: Path) -> bytes:
    """从自解压载体尾部取出控制面 ZIP 字节。"""
    try:
        raw = carrier.read_bytes()
    except OSError as exc:
        raise ToolError(
            f"❌ 无法读取安装包: {carrier}（{exc}）\n"
            f"👉 修复建议: 确认安装包未被杀毒软件隔离，或重新获取一份后再双击。"
        ) from exc

    if PAYLOAD_MARKER not in raw:
        raise ToolError(
            f"❌ 安装包内未找到载荷标记: {carrier}\n"
            f"👉 修复建议: 该文件可能不是 Entropaxis 安装包，或在传输中被截断，请重新获取。"
        )

    try:
        return base64.b64decode(raw.rsplit(PAYLOAD_MARKER, 1)[1])
    except (binascii.Error, ValueError) as exc:
        raise ToolError(
            f"❌ 安装包载荷解码失败: {exc}\n"
            f"👉 修复建议: 文件在传输中损坏（常见于聊天软件压缩），请重新获取原始安装包。"
        ) from exc


def _safe_members(archive: zipfile.ZipFile) -> list[str]:
    """校验并返回载荷成员清单，拒绝绝对路径与向上逃逸。"""
    names = archive.namelist()
    for name in names:
        posix = name.replace("\\", "/")
        if posix.startswith("/") or ".." in Path(posix).parts or (len(posix) > 1 and posix[1] == ":"):
            raise ToolError(
                f"❌ 安装包内含越界路径成员: {name}\n"
                f"👉 修复建议: 该安装包不可信，请从可信来源重新获取，勿继续安装。"
            )
    return names


def _merge_tree(stage: Path, dest: Path) -> int:
    """把暂存树覆盖合并进目标目录：同名文件覆盖，目标侧多出的文件原样保留。

    不用「先删后写」，是因为收件方可能在 `.system/skills/` 下放了自己的私有能力——
    整目录替换会把它们一并抹掉，而覆盖合并只动本安装包确实带来的那些文件。
    """
    written = 0
    for src in sorted(stage.rglob("*")):
        if not src.is_file():
            continue
        target = dest / src.relative_to(stage)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        written += 1
    return written


def install(payload: bytes, target_root: Path) -> dict:
    """把控制面装进 `target_root/.system`，返回结构化结果。"""
    root = Path(target_root).expanduser().resolve()
    if root.name == ".system":
        raise ToolError(
            f"❌ 选中的是控制面目录本身: {root}\n"
            f"👉 修复建议: 请选它的上一层（工作区根目录），安装程序会自动建立 .system 子目录。"
        )
    if root.exists() and not root.is_dir():
        raise ToolError(
            f"❌ 目标路径不是目录: {root}\n"
            f"👉 修复建议: 请重新选择一个文件夹作为工作区根目录。"
        )

    dest = root / ".system"
    upgrade = dest.is_dir()

    with tempfile.TemporaryDirectory(prefix="entropaxis-install-") as tmpdir:
        stage = Path(tmpdir) / ".system"
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                _safe_members(archive)
                archive.extractall(stage)
        except zipfile.BadZipFile as exc:
            raise ToolError(
                f"❌ 安装包载荷不是有效的压缩包: {exc}\n"
                f"👉 修复建议: 文件在传输中损坏，请重新获取原始安装包。"
            ) from exc

        missing = [m for m in REQUIRED_MEMBERS if not (stage / m).is_file()]
        if missing:
            raise ToolError(
                f"❌ 安装包载荷不完整，缺少: {', '.join(missing)}\n"
                f"👉 修复建议: 该安装包构建有误，请让维护者重新执行 build_windows_installer.py 生成。"
            )

        try:
            root.mkdir(parents=True, exist_ok=True)
            written = _merge_tree(stage, dest)
        except OSError as exc:
            raise ToolError(
                f"❌ 写入安装目录失败: {dest}（{exc}）\n"
                f"👉 修复建议: 换一个当前账户有写入权限的目录（如「文档」下的新建文件夹），"
                f"或右键以管理员身份运行安装程序。"
            ) from exc

    return {
        "status": "success",
        "workspace": str(root),
        "system_dir": str(dest),
        "written_files": written,
        "mode": "upgrade" if upgrade else "install",
    }


def run_bootstrap(workspace: Path) -> dict:
    """在安装目录实跑初始化链路，返回退出码与输出尾部。"""
    script = workspace / ".system" / "tools" / "bootstrap.py"
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=BOOTSTRAP_TIMEOUT,
    )
    return {
        "returncode": proc.returncode,
        "output": (proc.stdout + proc.stderr).strip(),
    }


def choose_directory(default: Path) -> Path | None:
    """弹出目录选择框；图形环境不可用时软降级为命令行输入并留痕。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        print("⚠️ 未检测到图形组件 tkinter，已降级为命令行输入。")
    else:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        picked = filedialog.askdirectory(
            title="选择 Entropaxis 工作区安装目录",
            initialdir=str(default),
            mustexist=False,
        )
        root.destroy()
        return Path(picked) if picked else None

    raw = input(f"请输入安装目录（直接回车使用 {default}）: ").strip()
    return Path(raw) if raw else default


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Entropaxis Windows 安装程序：选目录、展开控制面并初始化",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--carrier", required=True, help="自解压载体 .bat 的路径（载荷来源）")
    parser.add_argument("--target", help="安装目录；省略时弹出图形化目录选择框")
    parser.add_argument("--json", action="store_true", help="以结构化 JSON 格式输出结果")

    args = parser.parse_args()

    try:
        payload = read_payload(Path(args.carrier).expanduser().resolve())

        if args.target:
            target = Path(args.target)
        else:
            target = choose_directory(Path.home() / "Documents")
            if target is None:
                print("已取消安装，未做任何改动。")
                return 0

        res = install(payload, target)
        boot = run_bootstrap(Path(res["workspace"]))
        res["bootstrap"] = boot

        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if boot["returncode"] == 0 else 1

        verb = "升级" if res["mode"] == "upgrade" else "安装"
        print(f"✅ 控制面{verb}完成：{res['system_dir']}（{res['written_files']} 个文件）")
        print(boot["output"])
        if boot["returncode"] != 0:
            print(
                f"\n❌ 初始化未通过（退出码 {boot['returncode']}）。\n"
                f"👉 修复建议: 把上方报错整段复制给 AI 助手处理，"
                f"或在安装目录双击 .system\\tools\\一键配置工作区.bat 重试。",
                file=sys.stderr,
            )
            return 1
        print(f"\n🎉 工作区就绪：{res['workspace']}")
        print("   用 Claude Code / OMP / Cursor 打开这个目录，直接说话即可开始。")
        return 0
    except ToolError as err:
        print(str(err), file=sys.stderr)
        return 1
    except Exception as err:  # noqa: BLE001
        print(
            f"❌ 意外系统异常: {err}\n👉 修复建议: 请检查环境权限或汇报至根系统治理流程。",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
