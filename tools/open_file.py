#!/usr/bin/env python3
"""Open workspace files through the configured application, with visible fallback."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

try:
    from . import paths
except ImportError:
    import paths

WORKSPACE_ROOT = paths.WORKSPACE_ROOT
DEFAULT_CONFIG_PATH = paths.DATA_DIR / "templates" / "file-opener.json"


def _load_config(path: Path) -> tuple[dict, str]:
    if not path.is_file():
        return {}, f"打开器配置不存在: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"打开器配置无法读取: {exc}"
    if not isinstance(data.get("associations"), dict):
        return {}, f"打开器配置缺少 associations: {path}"
    return data, ""


def _association_for(path: Path, config: dict) -> dict | None:
    if path.is_dir():
        # 目录此前一律无关联项，每次打开都记一次「降级」——而降级是要逐项告知用户的
        # （文件交付 §4.5）。把天天发生、且本来就正确的路径记成异常，会训练人忽略降级提示。
        assoc = config.get("associations", {}).get("directory")
        return assoc if isinstance(assoc, dict) and assoc.get("command") else None
    if not path.suffix:
        return None
    extension = path.suffix.lower()
    for association in config.get("associations", {}).values():
        if not isinstance(association, dict):
            continue
        extensions = association.get("extensions", [])
        if extension in {str(item).lower() for item in extensions}:
            return association
    return None


def _configured_argv(command: str, path: Path, platform: str) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command 必须是非空字符串")
    argv = shlex.split(command)
    if platform == "win32" and argv[0].lower() == "start":
        return ["cmd", "/c", *argv, str(path)]
    return [*argv, str(path)]


def _default_argv(path: Path, platform: str) -> list[str]:
    if platform == "darwin":
        return ["open", str(path)]
    if platform == "win32":
        if path.is_dir():
            return ["explorer", str(path)]
        return ["cmd", "/c", "start", "", str(path)]
    return ["xdg-open", str(path)]


def _run(argv: list[str], runner) -> tuple[int, str]:
    try:
        completed = runner(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    detail = (completed.stderr or completed.stdout or "").strip()
    return completed.returncode, detail


OFFICE_EXTENSIONS = {
    ".xlsx", ".xls", ".xlsm", ".csv",
    ".docx", ".doc",
    ".pptx", ".ppt",
    ".pdf",
}


def _close_if_already_open(path: Path, platform: str, closer=subprocess.run) -> bool:
    """若办公文档已在桌面办公应用中打开，则先关闭对应窗口以保证后续打开加载磁盘最新内容。"""
    if not path.is_file() or path.suffix.lower() not in OFFICE_EXTENSIONS:
        return False
    # AppleScript 字符串字面量转义：文件名含 " 或 \ 时不转义会拼出语法错误的脚本，
    # 而错误会被下方 except 吞掉，表现为「永远刷新不了」这种无声失效。
    file_name = path.name.replace("\\", "\\\\").replace('"', '\\"')
    if platform == "darwin":
        # 必须先把目标进程激活到前台再枚举窗口：WPS 等 Qt/非原生应用只在 frontmost 时
        # 才惰性暴露 AX 窗口树，后台查询恒返回 0 个窗口，导致关闭逻辑静默空转。
        script = f'''
        tell application "System Events"
            set procNames to name of every process
            set officeApps to {{"wpsoffice", "WPS Office", "Microsoft Excel", "Microsoft Word", "Microsoft PowerPoint", "Preview"}}
            set closedAny to false
            repeat with p in procNames
                if p is in officeApps then
                    try
                        set frontmost of process p to true
                        -- 自适应等待 AX 窗口树就绪，最多 1.5s；就绪即走，不做固定长睡眠
                        repeat 10 times
                            if (count of windows of process p) > 0 then exit repeat
                            delay 0.15
                        end repeat
                        tell process p
                            repeat with w in (every window whose name contains "{file_name}")
                                try
                                    click (first button whose subrole is "AXCloseButton") of w
                                    set closedAny to true
                                end try
                            end repeat
                        end tell
                    end try
                end if
            end repeat
            return closedAny
        end tell
        '''
        try:
            res = closer(["osascript", "-e", script], check=False, capture_output=True, text=True, timeout=15)
            if res.returncode == 0 and "true" in (res.stdout or "").lower():
                time.sleep(0.3)
                return True
        except Exception:
            pass
    return False


def _open_path(
    path_text: str,
    config: dict,
    config_reason: str,
    platform: str,
    runner,
    closer=subprocess.run,
) -> dict:
    path = Path(path_text).expanduser().resolve()
    result = {
        "path": str(path),
        "ok": False,
        "selected_app": "",
        "command": [],
        "used_fallback": False,
        "reloaded": False,
        "reason": "",
        "returncode": 1,
    }
    if not path.exists():
        result["reason"] = f"目标不存在: {path}"
        return result

    # 若文件已打开，先关闭已有窗口触发热刷新
    result["reloaded"] = _close_if_already_open(path, platform, closer=closer)

    association = _association_for(path, config)
    command = association.get("command", "") if association else ""
    if command:
        try:
            configured_argv = _configured_argv(command, path, platform)
        except ValueError as exc:
            configured_argv = []
            returncode, detail = 2, f"配置命令无法解析: {exc}"
        else:
            returncode, detail = _run(configured_argv, runner)
        result.update({
            "selected_app": association.get("selected_app", "已配置程序"),
            "command": configured_argv,
            "returncode": returncode,
        })
        if returncode == 0:
            result["ok"] = True
            return result
        reason = f"配置程序执行失败（退出码 {returncode}）"
        if detail:
            reason += f": {detail}"
    elif config_reason:
        reason = config_reason
    elif path.is_dir():
        reason = "目录未配置专用打开器"
    elif not path.suffix:
        reason = "无扩展名目标未配置专用打开器"
    else:
        reason = f"扩展名 {path.suffix.lower()} 未配置打开器"

    fallback_argv = _default_argv(path, platform)
    if result["command"] == fallback_argv:
        result["reason"] = reason
        return result

    returncode, detail = _run(fallback_argv, runner)
    result.update({
        "ok": returncode == 0,
        "selected_app": "系统默认关联程序",
        "command": fallback_argv,
        "used_fallback": True,
        "reason": reason,
        "returncode": returncode,
    })
    if returncode != 0 and detail:
        result["reason"] += f"；系统默认程序也失败: {detail}"
    return result

def open_paths(
    paths: list[str],
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    platform: str = sys.platform,
    runner=subprocess.run,
    closer=subprocess.run,
) -> list[dict]:
    """Open every path independently; return one observable result per path."""
    config, config_reason = _load_config(Path(config_path))
    return [_open_path(path, config, config_reason, platform, runner, closer=closer) for path in paths]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按 .entropaxis/data/templates/file-opener.json 打开文件；仅在配置缺失或失败时降级。"
    )
    parser.add_argument("paths", nargs="+", help="要打开的文件或目录，可一次传入多个")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="打开器配置路径")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = parser.parse_args()

    results = open_paths(args.paths, config_path=args.config)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            reload_hint = "（已刷新重新加载）" if result.get("reloaded") else ""
            if result["ok"] and result["used_fallback"]:
                print(f"[WARN] 已使用系统默认程序打开 {result['path']}{reload_hint}；原因: {result['reason']}")
            elif result["ok"]:
                print(f"[OK] 已使用 {result['selected_app']} 打开 {result['path']}{reload_hint}")
            else:
                print(f"[ERROR] 无法打开 {result['path']}；原因: {result['reason']}", file=sys.stderr)
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
