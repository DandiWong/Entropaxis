#!/usr/bin/env python3
"""Windows 安装程序本体：选目录 → 展开控制面 → 跑初始化。

由 GitHub Actions 用 PyInstaller 冻结为单文件 exe；控制面 ZIP 作为随包数据躺在
`sys._MEIPASS`，安装全程不联网、不拉远端。

两条安全边界（均为信任边界，不做简化）：
  1. **只写 `.entropaxis/`（不含 `data/` 子目录）**：`.entropaxis/data/` 是用户实例数据与
     凭据所在，安装与升级全程不触碰；
  2. **载荷成员路径校验**：载荷虽由本仓库 `git archive` 产出，但经过随包封装后即为
     外部输入，解压前逐条排除绝对路径与 `..`（Zip Slip）；

初始化不拿 `sys.executable` 起子进程跑 `bootstrap.py`：冻结后 `sys.executable` 指向安装
程序自己，子进程会变成递归重启安装程序；收件方此刻也未必已装系统 Python。改用 `runpy`
在本进程内执行。

遵循 ApX 工具契约：纯标准库、行动导向错误、结构化输出、临时目录原子组装后再落盘。
"""

from __future__ import annotations

import argparse
import io
import json
import runpy
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


def _ensure_utf8_console() -> None:
    """中文 Windows 的默认控制台代码页是 cp936/cp1252，不是 UTF-8。

    本程序的提示与报错全是中文，双击运行时若不重配编码，`print()` 遇到中文字符会直接
    抛 UnicodeEncodeError 崩掉——用户看到的不是安装失败的原因，而是安装程序自己先炸了。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

# 随包数据的文件名，构建侧（build_windows_installer.py）与安装侧共用本常量。
PAYLOAD_NAME = "entropaxis-payload.zip"

# 载荷完整性判据：缺其一即说明拿到的不是一份完整控制面，宁可阻断也不落一半。
REQUIRED_MEMBERS = ("tools/bootstrap.py", "entrypoints/AGENTS.md", "entrypoints/CLAUDE.md")

# 本文件不导入 tools/paths.py：PyInstaller --onefile 只冻结本脚本的导入图，paths.py 是
# 随包 ZIP 里的数据，安装完成前不在 sys.path 上；因此控制面目录名与 paths.SYSTEM_DIRNAME
# 各自独立声明，改名时两处需同步更新（单元测试 test_windows_installer.py 兜底核验一致）。
SYSTEM_DIRNAME = ".entropaxis"
LEGACY_SYSTEM_DIRNAME = ".system"
LEGACY_DATA_DIRNAME = ".data"


class ToolError(Exception):
    """工具可恢复业务异常，包含行动导向修复指引。"""


def resolve_payload() -> bytes:
    """从冻结 exe 的随包数据里取出控制面 ZIP 字节；非冻结形态直接阻断。"""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        raise ToolError(
            "❌ 未检测到随包控制面载荷：本程序需以 GitHub Actions 构建的 exe 形态运行。\n"
            "👉 修复建议: 从 Releases 或 Actions 制品获取「Entropaxis安装程序.exe」并直接双击，"
            "勿直接运行本源码文件。"
        )
    path = Path(base) / PAYLOAD_NAME
    if not path.is_file():
        raise ToolError(
            f"❌ 安装程序内未随包控制面载荷（缺 {PAYLOAD_NAME}）。\n"
            f"👉 修复建议: 该 exe 构建有误，请用 build-windows-installer 工作流重新构建。"
        )
    return path.read_bytes()


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

    不用「先删后写」，是因为收件方可能在 `.entropaxis/skills/` 下放了自己的私有能力——
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


def detect_legacy_layout(root: Path) -> list[str]:
    """检测目标工作区根目录下是否残留旧布局（.system/ 或 .data/），只报现象不动手。

    两种成因（旧版本升级遗留 / 某写入方未按契约在根目录误建）现场无法区分，
    统一报现象并交由用户人工迁移，不在安装流程中顺带处理。
    """
    return [name for name in (LEGACY_SYSTEM_DIRNAME, LEGACY_DATA_DIRNAME) if (root / name).is_dir()]


def install(payload: bytes, target_root: Path) -> dict:
    """把控制面装进 `target_root/.entropaxis`，返回结构化结果。"""
    root = Path(target_root).expanduser().resolve()
    if root.name == SYSTEM_DIRNAME:
        raise ToolError(
            f"❌ 选中的是控制面目录本身: {root}\n"
            f"👉 修复建议: 请选它的上一层（工作区根目录），安装程序会自动建立 {SYSTEM_DIRNAME} 子目录。"
        )
    if root.exists() and not root.is_dir():
        raise ToolError(
            f"❌ 目标路径不是目录: {root}\n"
            f"👉 修复建议: 请重新选择一个文件夹作为工作区根目录。"
        )

    legacy = detect_legacy_layout(root)

    dest = root / SYSTEM_DIRNAME
    # 判据看 tools/ 而非 dest 本身：data/ 现已嵌套进 dest 内，用户只有实例数据、
    # 控制面从未落地时 dest 已存在但只含 data/——那仍是"安装"，不是"升级"。
    upgrade = (dest / "tools").is_dir()

    with tempfile.TemporaryDirectory(prefix="entropaxis-install-") as tmpdir:
        stage = Path(tmpdir) / SYSTEM_DIRNAME
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
        "legacy_layout": legacy,
    }


def run_bootstrap(workspace: Path) -> dict:
    """在本进程内跑安装目录里的 `bootstrap.py`，返回退出码与失败原因。

    刻意不起子进程：冻结成 exe 后 `sys.executable` 是安装程序自己，子进程会递归重启安装
    流程；而收件方此刻很可能还没装系统 Python，也没有第二个解释器可用。`bootstrap.py` 是
    纯标准库、从 `__file__` 派生工作区根，`runpy` 执行它即可拿到正确的安装目录。
    """
    script = workspace / SYSTEM_DIRNAME / "tools" / "bootstrap.py"
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        return {"returncode": int(exc.code or 0), "error": ""}
    except Exception as exc:  # noqa: BLE001 - 初始化失败不应吞掉已完成的安装
        return {"returncode": 1, "error": f"{type(exc).__name__}: {exc}"}
    return {"returncode": 0, "error": ""}


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
    _ensure_utf8_console()
    parser = argparse.ArgumentParser(
        description="Entropaxis Windows 安装程序：选目录、展开控制面并初始化",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--target", help="安装目录；省略时弹出图形化目录选择框")
    parser.add_argument("--json", action="store_true", help="以结构化 JSON 格式输出结果")

    args = parser.parse_args()

    try:
        payload = resolve_payload()

        if args.target:
            target = Path(args.target)
        else:
            target = choose_directory(Path.home() / "Documents")
            if target is None:
                print("已取消安装，未做任何改动。")
                return 0

        res = install(payload, target)
        verb = "升级" if res["mode"] == "upgrade" else "安装"
        if not args.json:
            print(f"✅ 控制面{verb}完成：{res['system_dir']}（{res['written_files']} 个文件）")
            # 旧布局残留提示由随后 run_bootstrap 内的 print_legacy_layout_hint() 统一打印，
            # 此处不重复；legacy_layout 字段只为 --json 消费方保留结构化信号。

        boot = run_bootstrap(Path(res["workspace"]))
        res["bootstrap"] = boot

        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if boot["returncode"] == 0 else 1

        if boot["returncode"] != 0:
            print(
                f"\n❌ 控制面已就位，但初始化未通过：{boot['error'] or '退出码 ' + str(boot['returncode'])}\n"
                f"👉 修复建议: 装好 Python 3 后，在安装目录执行 "
                f"python .entropaxis\\tools\\bootstrap.py 重跑初始化；或把上方报错整段复制给 AI 助手处理。",
                file=sys.stderr,
            )
            return 1
        print(f"\n🎉 工作区就绪：{res['workspace']}")
        print("   用 Claude Code / OMP / Cursor 打开这个目录，直接说话即可开始。")
        print("   日常使用仍需本机装有 Python 3（工作区的工具都是 .py）。")
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
    code = main()
    # 冻结成 exe 时双击运行，控制台窗口会随进程退出一并消失——成功提示和报错都来不及看。
    # 但非交互调用（CI 自检、--json 被另一进程捕获输出）没有可读的 stdin，input() 会遇 EOF
    # 崩出未捕获异常，把已经算好的退出码打翻——这不是使用者的错，不该让程序在这里再炸一次。
    if getattr(sys, "frozen", False) and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\n按回车键退出...")
        except (EOFError, OSError):
            pass
    sys.exit(code)
