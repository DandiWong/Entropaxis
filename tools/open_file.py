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


MISSING_PARTITION_REASON = "本平台未配置打开器，运行 bootstrap.py 生成"


def _load_config(path: Path, platform: str) -> tuple[dict, str]:
    """只读当前平台分区；分区缺失时返回空映射，由调用方降级为系统默认程序并留痕。"""
    if not path.is_file():
        return {}, f"打开器配置不存在: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"打开器配置无法读取: {exc}"
    if isinstance(data.get("platforms"), dict):
        partition = data["platforms"].get(platform)
        if isinstance(partition, dict) and isinstance(partition.get("associations"), dict):
            return partition, ""
        return {}, MISSING_PARTITION_REASON
    # 兼容旧版单平台格式（顶层 associations）：仅当其 platform 字段（缺失视为当前平台）
    # 与本机一致时才生效，避免工作区跨平台搬运后把他平台的命令当本平台配置执行
    if isinstance(data.get("associations"), dict) and data.get("platform", platform) == platform:
        return data, ""
    return {}, MISSING_PARTITION_REASON


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

DELIVERY_AUTO_OPEN_EXTENSIONS = {
    # 办公文档
    ".docx", ".doc", ".xlsx", ".xls", ".csv", ".pptx", ".ppt", ".pdf",
    # 视觉图表
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    # 网页原型（.md/.yaml/.json/.adoc 等中间产物不在此列，仅显式打开时才打开）
    ".html", ".htm",
}

VERIFY_TIMEOUT_S = 20


def _target_office_apps(suffix: str, target_cmd: str = "", selected_app: str = "") -> list[str]:
    """根据文件格式及配置的目标打开程序，收敛可能承载旧窗口的办公进程列表。"""
    combined = (target_cmd + " " + selected_app).lower()
    # 明确为非原生桌面办公应用（如终端、编辑器、浏览器、Orca）时无需探测 AX 窗口
    if any(k in combined for k in ("orca", "code", "cursor", "subl", "vim", "nano", "chrome", "safari")):
        return []
    if "wps" in combined:
        return ["wpsoffice", "WPS Office"]
    if "word" in combined:
        return ["Microsoft Word", "wpsoffice", "WPS Office"]
    if "excel" in combined:
        return ["Microsoft Excel", "wpsoffice", "WPS Office"]
    if "powerpoint" in combined or "wpp" in combined or "ppt" in combined:
        return ["Microsoft PowerPoint", "wpsoffice", "WPS Office"]
    if "preview" in combined:
        return ["Preview"]

    # 缺省按扩展名收敛相关办公套件
    ext = suffix.lower()
    if ext in {".docx", ".doc"}:
        return ["wpsoffice", "WPS Office", "Microsoft Word"]
    if ext in {".xlsx", ".xls", ".xlsm", ".csv"}:
        return ["wpsoffice", "WPS Office", "Microsoft Excel"]
    if ext in {".pptx", ".ppt"}:
        return ["wpsoffice", "WPS Office", "Microsoft PowerPoint"]
    if ext == ".pdf":
        return ["Preview", "wpsoffice", "WPS Office"]
    return []


def _close_if_already_open(
    path: Path,
    platform: str,
    closer=subprocess.run,
    target_cmd: str = "",
    selected_app: str = "",
) -> bool:
    """若办公文档已在桌面办公应用中打开，则先关闭对应窗口以保证后续打开加载磁盘最新内容。"""
    if not path.is_file() or path.suffix.lower() not in OFFICE_EXTENSIONS:
        return False
    target_apps = _target_office_apps(path.suffix, target_cmd=target_cmd, selected_app=selected_app)
    if not target_apps:
        return False

    file_name = path.name.replace("\\", "\\\\").replace('"', '\\"')
    apps_quoted = ", ".join(f'"{a}"' for a in target_apps)

    if platform == "darwin":
        # 必须先把目标进程激活到前台再枚举窗口：WPS 等 Qt/非原生应用只在 frontmost 时
        # 才惰性暴露 AX 窗口树，后台查询恒返回 0 个窗口，导致关闭逻辑静默空转。
        # 防御机制：预先记录原前台应用；探针结束前无条件把前台还给原应用，绝不残留空前台
        # （随后由打开命令自行激活目标应用）。
        script = f'''
        tell application "System Events"
            set origProc to missing value
            try
                set origProc to first application process whose frontmost is true
            end try
            set procNames to name of every process
            set officeApps to {{{apps_quoted}}}
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
            if origProc is not missing value then
                try
                    set frontmost of origProc to true
                end try
            end if
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


# AppleScript 回传格式：<状态>RS<标题>US<标题>US…；RS/US 为 ASCII 记录/单元分隔符，标题含换行或普通标点都不会被误切。
_RS, _US = 30, 31


def _parse_window_report(stdout: str) -> tuple[str, list[str]]:
    """解析窗口报告，返回 (状态, 标题列表)；状态为 noproc/noact/ok，格式异常为 bad（调用方按无法校验处理）。"""
    text = stdout or ""
    if text.endswith("\n"):
        text = text[:-1]  # osascript 在结果后追加的换行；标题内部换行原样保留
    status, sep, rest = text.partition(chr(_RS))
    if not sep or status not in {"noproc", "noact", "ok"}:
        return "bad", []
    return status, [t for t in rest.split(chr(_US)) if t.strip()]


def _title_hits(titles: list[str], file_name: str) -> bool:
    """任一窗口标题含目标文件名即命中（不区分大小写，与 AppleScript contains 一致）。"""
    needle = file_name.casefold()
    return bool(needle) and any(needle in t.casefold() for t in titles)


def _verify_window_open(
    path: Path,
    platform: str,
    closer=subprocess.run,
    target_cmd: str = "",
    selected_app: str = "",
    timeout_s: float | None = None,
) -> tuple[bool | None, str]:
    """打开命令成功后，有界轮询确认文档窗口。返回 (True|False|None, 说明)；None 表示无法校验。会激活目标应用且不还原前台。"""
    if platform != "darwin":
        return None, "本平台仅校验命令退出码"
    if not path.is_file() or path.suffix.lower() not in OFFICE_EXTENSIONS:
        return None, "该文件类型不做窗口校验"
    target_apps = _target_office_apps(path.suffix, target_cmd=target_cmd, selected_app=selected_app)
    if not target_apps:
        return None, "该打开程序不支持窗口校验，仅校验命令退出码"

    apps_quoted = ", ".join(f'"{a}"' for a in target_apps)
    # WPS 等 Qt 应用只在前台才暴露 AX 窗口树，故校验先激活目标应用；结束后不还原前台，让被打开的文档留在前台可见。
    # AppleScript 只负责激活并回传窗口标题（单次，不轮询）；命中判定与 VERIFY_TIMEOUT_S 轮询在 Python 侧，可被单测直接覆盖。
    script = f'''
    set officeApps to {{{apps_quoted}}}
    set RS to character id {_RS}
    set US to character id {_US}
    set procSeen to false
    set actOK to false
    set titles to ""
    tell application "System Events"
        set procNames to name of every process
        repeat with pn in procNames
            if (pn as text) is in officeApps then
                set procSeen to true
                try
                    if not (frontmost of process (pn as text)) then set frontmost of process (pn as text) to true
                    if frontmost of process (pn as text) then
                        set actOK to true
                        -- 只有 1 个窗口时 AppleScript 返回字符串而非列表，须先包成列表再遍历
                        set ws to name of every window of process (pn as text)
                        if class of ws is not list then set ws to {{ws}}
                        repeat with i from 1 to count of ws
                            try
                                set titles to titles & ((item i of ws) as text) & US
                            end try
                        end repeat
                    end if
                end try
            end if
        end repeat
    end tell
    if not procSeen then return "noproc" & RS
    if not actOK then return "noact" & RS
    return "ok" & RS & titles
    '''
    deadline = time.monotonic() + (VERIFY_TIMEOUT_S if timeout_s is None else timeout_s)
    never_activated = False
    while True:
        try:
            res = closer(["osascript", "-e", script], check=False, capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            return False, f"{VERIFY_TIMEOUT_S} 秒内未见文档窗口"
        except Exception as exc:
            return None, f"窗口校验无法执行: {exc}"
        err = res.stderr or ""
        if res.returncode != 0:
            if "-25211" in err or "assistive" in err.lower():
                return None, "缺少辅助功能权限，无法校验"
            return None, "窗口校验脚本出错，无法校验"
        status, titles = _parse_window_report(res.stdout)
        if status == "bad":
            return None, "窗口校验输出无法解析，无法校验"
        if status == "ok":
            never_activated = False
            if _title_hits(titles, path.name):
                return True, ""
        else:
            never_activated = status == "noact"
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if never_activated:
        return None, "无法激活目标应用读取窗口，已发出打开命令但未确认"
    return False, f"{VERIFY_TIMEOUT_S} 秒内未见文档窗口（可能仍在启动）"


def _apply_verify(result: dict, path: Path, platform: str, closer, command: str, selected_app: str) -> None:
    """命令成功后写入 verified/verify_note；校验为 False 时 ok 降为 False 并写明原因。"""
    verified, note = _verify_window_open(path, platform, closer, command, selected_app)
    result["verified"], result["verify_note"] = verified, note
    if verified is False:
        result["ok"] = False
        result["reason"] = note


BROWSER_RELOAD_EXTENSIONS = {".html", ".htm"}

# 每个浏览器单独一段脚本：未安装的浏览器编译失败只影响自己。`is running` 守卫防止把未开的浏览器拉起来。
_BROWSER_RELOAD_SCRIPTS = {
    "Google Chrome": '''
if application "Google Chrome" is running then
    tell application "Google Chrome"
        repeat with w in windows
            set i to 0
            repeat with t in tabs of w
                set i to i + 1
                set u to URL of t
                if u is "{url}" or u starts with "{url}/" or u starts with "{url}#" or u starts with "{url}?" then
                    reload t
                    set active tab index of w to i
                    set index of w to 1
                    activate
                    return "true"
                end if
            end repeat
        end repeat
    end tell
end if
return "false"
''',
    "Safari": '''
if application "Safari" is running then
    tell application "Safari"
        repeat with w in windows
            repeat with t in tabs of w
                set u to URL of t
                if u is "{url}" or u starts with "{url}/" or u starts with "{url}#" or u starts with "{url}?" then
                    set URL of t to u
                    set current tab of w to t
                    set index of w to 1
                    activate
                    return "true"
                end if
            end repeat
        end repeat
    end tell
end if
return "false"
''',
}


def _reload_browser_tab(url: str, platform: str, closer=subprocess.run) -> str:
    """已有同地址标签页时就地刷新并切到前台，返回浏览器名；没有则返回空串，由调用方照常打开。"""
    if platform != "darwin":
        return ""  # ponytail: 仅 macOS AppleScript；其他平台照旧新开，有需求再补
    escaped = url.rstrip("/").replace("\\", "\\\\").replace('"', '\\"')
    for browser, template in _BROWSER_RELOAD_SCRIPTS.items():
        try:
            res = closer(["osascript", "-e", template.replace("{url}", escaped)],
                         check=False, capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        if res.returncode == 0 and "true" in (res.stdout or "").lower():
            return browser
    return ""


def _open_path(
    path_text: str,
    config: dict,
    config_reason: str,
    platform: str,
    runner,
    closer=subprocess.run,
    on_deliver: bool = False,
) -> dict:
    if path_text.startswith(("http://", "https://")):
        browser = _reload_browser_tab(path_text, platform, closer)
        if browser:
            return {
                "path": path_text, "ok": True, "selected_app": browser, "command": [],
                "used_fallback": False, "reloaded": True, "reason": "", "returncode": 0,
                "verified": True, "verify_note": "",
            }
        # 本地 dev 页面预览：URL 无扩展名映射，直接交给系统默认浏览器
        argv = {"darwin": ["open", path_text], "win32": ["cmd", "/c", "start", "", path_text]}.get(
            platform, ["xdg-open", path_text]
        )
        returncode, detail = _run(argv, runner)
        return {
            "path": path_text,
            "ok": returncode == 0,
            "selected_app": "系统默认浏览器",
            "command": argv,
            "used_fallback": False,
            "reloaded": False,
            "reason": detail if returncode else "",
            "returncode": returncode,
            "verified": None,
            "verify_note": "URL 由默认浏览器打开，不做窗口校验",
        }
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
        "verified": None,
        "verify_note": "",
    }
    if not path.exists():
        result["reason"] = f"目标不存在: {path}"
        return result

    if on_deliver:
        if path.is_dir():
            result.update({
                "ok": True,
                "skipped": True,
                "selected_app": "跳过（交付不自动打开目录）",
                "reason": "交付场景不自动打开目录，已在回复中给出路径",
                "returncode": 0,
            })
            return result
        suffix = path.suffix.lower()
        if suffix not in DELIVERY_AUTO_OPEN_EXTENSIONS:
            result.update({
                "ok": True,
                "skipped": True,
                "selected_app": "跳过（非交付自动打开格式）",
                "reason": f"交付场景不自动打开该格式 ({suffix or '无扩展名'})，已在回复中给出路径",
                "returncode": 0,
            })
            return result

    if path.is_file() and path.suffix.lower() in BROWSER_RELOAD_EXTENSIONS:
        browser = _reload_browser_tab(path.as_uri(), platform, closer)
        if browser:
            result.update({"ok": True, "selected_app": browser, "reloaded": True, "returncode": 0, "verified": True})
            return result

    association = _association_for(path, config)
    command = association.get("command", "") if association else ""
    selected_app = association.get("selected_app", "") if association else ""

    # 若文件已打开，先关闭已有窗口触发热刷新（按配置目标应用精准收敛，未关闭时自动还原前台应用）
    result["reloaded"] = _close_if_already_open(
        path, platform, closer=closer, target_cmd=command, selected_app=selected_app
    )
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
            _apply_verify(result, path, platform, closer, command, selected_app)
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
    if returncode == 0:
        _apply_verify(result, path, platform, closer, "", "")
    return result

def open_paths(
    paths: list[str],
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    platform: str = sys.platform,
    runner=subprocess.run,
    closer=subprocess.run,
    on_deliver: bool = False,
) -> list[dict]:
    """Open every path independently; return one observable result per path."""
    config, config_reason = _load_config(Path(config_path), platform)
    return [_open_path(path, config, config_reason, platform, runner, closer=closer, on_deliver=on_deliver) for path in paths]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按 .entropaxis/data/templates/file-opener.json 打开文件；仅在配置缺失或失败时降级。"
    )
    parser.add_argument("paths", nargs="+", help="要打开的文件或目录，可一次传入多个")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="打开器配置路径")
    parser.add_argument(
        "--on-deliver",
        action="store_true",
        help="交付落盘模式：仅对白名单格式（办公文档/图表/原型等）自动打开，.md 等中间产物（.yaml/.json/.adoc 等）与目录静默跳过",
    )
    args = parser.parse_args()

    results = open_paths(args.paths, config_path=args.config, on_deliver=args.on_deliver)
    for result in results:
        reload_hint = "（已刷新重新加载）" if result.get("reloaded") else ""
        confirmed = result.get("verified") is True
        # 只有 verified=True 才允许完成式「已…打开」；其余成功一律「发出打开命令，未确认窗口」
        unverified = f"（未确认窗口：{result.get('verify_note') or '未校验'}）"
        if result.get("skipped"):
            print(f"[SKIP] {result['path']}（{result['reason']}）")
        elif result["ok"] and result["used_fallback"] and confirmed:
            print(f"[WARN] 已使用系统默认程序打开 {result['path']}{reload_hint}（已确认文档窗口）；原因: {result['reason']}")
        elif result["ok"] and result["used_fallback"]:
            print(f"[WARN] 已用系统默认程序向 {result['path']} 发出打开命令{unverified}；原因: {result['reason']}")
        elif result["ok"] and confirmed:
            print(f"[OK] 已使用 {result['selected_app']} 打开 {result['path']}{reload_hint}（已确认文档窗口）")
        elif result["ok"]:
            print(f"[OK] 已向 {result['selected_app']} 发出打开命令 {result['path']}{unverified}")
        elif result.get("verified") is False:
            print(f"❌ 未确认打开 {result['path']}；原因: {result['reason']}", file=sys.stderr)
        else:
            print(f"❌ 无法打开 {result['path']}；原因: {result['reason']}\n"
                  "👉 修复建议: 核对路径；打开程序配置在 .entropaxis/data/templates/file-opener.json。", file=sys.stderr)
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
