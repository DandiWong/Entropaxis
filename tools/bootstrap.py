#!/usr/bin/env python3
"""
工作区根入口与系统配置初始化工具 (Bootstrap)
用于一键同步工作区根目录的 AGENTS.md / CLAUDE.md 入口文件，自动检测宿主机安装的应用程序，
生成/维护各类文件格式的默认打开器关联配置 (.data/file-opener.json)，并自适应引导环境。
"""
import os
import sys
import json
import shutil
from pathlib import Path
def render_instance_configs(
    verbose: bool = True,
    *,
    templates_dir: Path | None = None,
    data_dir: Path | None = None,
    ws_name: str | None = None,
) -> bool:
    """从 .system/templates/*.template.{json,md} 首次渲染到 .data/。

    - 仅在 .data/ 目标文件完全缺失时写入；存在即不动（避免覆盖用户已填内容）
    - 占位符 {{XXX}} 替换为工作区目录名兜底（无脑填充，明示待填）
    - 不阻断、不抛错；模板文件缺失时跳过
    - 写入用 tempfile + replace 实现原子替换
    - 三个路径/名称参数均可由测试覆写；生产调用全部传 None，从 __file__ 派生
    """
    import tempfile

    if templates_dir is None or data_dir is None:
        tools_dir = Path(__file__).resolve().parent
        system_dir = tools_dir.parent
        ws_root = system_dir.parent
        if templates_dir is None:
            templates_dir = system_dir / "templates"
        if data_dir is None:
            data_dir = ws_root / ".data"
    data_dir.mkdir(parents=True, exist_ok=True)

    if ws_name is None:
        ws_name = data_dir.parent.name or "workspace"

    success = True
    rendered = 0


    # 仅渲染真正属于 .data/ 的实例模板；templates/ 目录下还有项目级脚手架模板（README/AGENTS 等），由 init_project / init_app 走，不在此处处理
    DATA_INSTANCE_TEMPLATES = {"board_config.template.json", "workspace-config.template.md"}


    for tpl in sorted(templates_dir.glob("*.template.json")):
        if tpl.name not in DATA_INSTANCE_TEMPLATES:
            continue
        target_name = tpl.name.replace(".template.json", ".json")
        target = data_dir / target_name
        if target.exists():
            continue
        try:
            tpl_data = json.loads(tpl.read_text(encoding="utf-8"))
            target.write_text(
                json.dumps(tpl_data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            rendered += 1
            if verbose:
                print(f"✅ 已从模板渲染 .data/{target_name}（首次，空 providers）")
        except Exception as exc:
            if verbose:
                print(f"❌ 渲染 {target_name} 失败: {exc}", file=sys.stderr)
            success = False
    for tpl in sorted(templates_dir.glob("*.template.md")):
        if tpl.name not in DATA_INSTANCE_TEMPLATES:
            continue


        target_name = tpl.name.replace(".template.md", ".md")
        target = data_dir / target_name
        if target.exists():
            continue
        try:
            content = tpl.read_text(encoding="utf-8")
            # 占位符替换为工作区目录名兜底（明示待填）
            rendered_content = content.replace("{{ORG_FULL_NAME}}", f"{ws_name}（待填：组织完整名称）")
            rendered_content = rendered_content.replace("{{ORG_FORBIDDEN_ABBR}}", f"{ws_name}-abbr（待填：禁用缩写）")
            rendered_content = rendered_content.replace("{{ORG_REVIEWER_CLI}}", "omp（待填：reviewer CLI）")
            rendered_content = rendered_content.replace("{{ORG_REVIEWER_CMD}}", "omp --model <待填>（待填：启动命令）")
            rendered_content = rendered_content.replace("{{ORG_SHARED_DIR_1}}", "01公司资料（待填：共享资料目录1）")
            rendered_content = rendered_content.replace("{{ORG_SHARED_PURPOSE_1}}", "待填：用途")
            rendered_content = rendered_content.replace("{{ORG_SHARED_DIR_2}}", "02部门资料（待填：共享资料目录2）")
            rendered_content = rendered_content.replace("{{ORG_SHARED_PURPOSE_2}}", "待填：用途")
            rendered_content = rendered_content.replace("{{ORG_SHARED_DIR_3}}", "03个人资料（待填：共享资料目录3）")
            rendered_content = rendered_content.replace("{{ORG_SHARED_PURPOSE_3}}", "待填：用途")
            # 原子写入
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", delete=False, dir=str(data_dir), prefix=f".{target_name}.tmp."
            ) as tmp:
                tmp.write(rendered_content)
                tmp_path = Path(tmp.name)
            tmp_path.replace(target)
            rendered += 1
            if verbose:
                print(f"✅ 已从模板渲染 .data/{target_name}（首次，占位符已替换为目录名兜底）")
        except Exception as exc:
            if verbose:
                print(f"❌ 渲染 {target_name} 失败: {exc}", file=sys.stderr)
            success = False

    if verbose and rendered == 0:
        print("ℹ️ .data/ 实例模板无需渲染（目标文件已全部存在）")
    return success


def sync_root_configs(verbose: bool = True) -> bool:
    """
    将 .system/root-configs 下的入口配置物理同步至工作区根目录。
    为避免 Synology Drive / 云同步网盘在跨平台同步时对软链接产生 Conflict 冲突，
    采用幂等文件复制（shutil.copy2）作为标准同步策略。
    """
    tools_dir = Path(__file__).resolve().parent
    system_dir = tools_dir.parent
    ws_root = system_dir.parent
    root_configs = system_dir / "root-configs"

    if not root_configs.exists():
        if verbose:
            print(f"❌ 错误: 未找到配置源目录 {root_configs}", file=sys.stderr)
        return False

    success = True
    for filename in ["AGENTS.md", "CLAUDE.md"]:
        src = root_configs / filename
        dst = ws_root / filename

        if not src.exists():
            continue

        try:
            # 如果目标是软链接或已存在文件，直接清理后复制，消除网盘软链接冲突
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            shutil.copy2(src, dst)
            if verbose:
                print(f"✅ 入口文件同步就绪: {filename} <- .system/root-configs/{filename}")
        except Exception as e:
            if verbose:
                print(f"❌ 入口同步失败 ({filename}): {e}", file=sys.stderr)
            success = False

    return success


# 兼容旧接口命名
setup_symlinks = sync_root_configs

def check_dashboard_token() -> bool:
    """检查看板凭证是否就绪"""
    ws_cred = Path(__file__).resolve().parent.parent.parent / ".data" / "credentials"
    if ws_cred.is_dir():
        for p in ws_cred.glob("*/token"):
            if p.is_file() and len(p.read_text(encoding="utf-8").strip()) > 10:
                return True
    config_dir = Path.home() / ".config"
    if config_dir.is_dir():
        for p in config_dir.glob("*dashboard*/token"):
            if p.is_file() and len(p.read_text(encoding="utf-8").strip()) > 10:
                return True
    return False

def detect_host_apps() -> set[str]:
    """检测宿主机已安装的应用程序"""
    detected = set()
    if sys.platform == "darwin":
        app_dirs = [Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications"]
        for adir in app_dirs:
            if adir.is_dir():
                for p in adir.glob("*.app"):
                    detected.add(p.stem.lower())
                for p in adir.glob("*/*.app"):
                    detected.add(p.stem.lower())
    elif sys.platform == "win32":
        prog_dirs = [
            Path(os.environ.get("ProgramFiles", "C:\\Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"
        ]
        for pdir in prog_dirs:
            if pdir.is_dir():
                try:
                    for p in pdir.glob("**/*.exe"):
                        detected.add(p.stem.lower())
                except Exception:
                    pass
    else:  # linux
        for pdir in [Path("/usr/share/applications"), Path.home() / ".local/share/applications"]:
            if pdir.is_dir():
                for p in pdir.glob("*.desktop"):
                    detected.add(p.stem.lower())
    return detected

def _opener_cmd_key() -> str:
    if sys.platform == "darwin":
        return "cmd_macos"
    if sys.platform == "win32":
        return "cmd_windows"
    return "cmd_linux"

def _build_opener_defaults(tpl_data: dict, detected_apps: set[str], cmd_key: str, verbose: bool = False) -> dict:
    """依据模板与本机检测结果构建全量打开器配置（纯构建，不含任何写入）。"""
    config = {
        "version": tpl_data.get("version", "1.0.0"),
        "platform": sys.platform,
        "associations": {},
    }
    if verbose:
        print("🔍 正在检测本机安装的应用程序以配置默认打开程序...")
    for fmt_id, fmt_info in tpl_data.get("formats", {}).items():
        selected_cmd = ""
        selected_app_name = ""
        matched_candidates = []
        for cand in fmt_info.get("candidates", []):
            cand_name = cand["name"]
            cand_cmd = cand.get(cmd_key, "")
            if not cand_cmd:
                continue
            cand_lower = cand_name.lower().replace(" ", "").replace("office", "")
            is_installed = False
            for dapp in detected_apps:
                dapp_clean = dapp.replace(" ", "").replace("office", "")
                if cand_lower in dapp_clean or dapp_clean in cand_lower:
                    is_installed = True
                    break
            if is_installed or "默认" in cand_name or "系统" in cand_name:
                matched_candidates.append({"name": cand_name, "command": cand_cmd, "is_installed": is_installed})
                if not selected_cmd and is_installed:
                    selected_cmd = cand_cmd
                    selected_app_name = cand_name
        if not selected_cmd:
            if sys.platform == "darwin":
                selected_cmd = "open"
            elif sys.platform == "win32":
                selected_cmd = "start \"\""
            else:
                selected_cmd = "xdg-open"
            selected_app_name = "系统默认关联程序"
        config["associations"][fmt_id] = {
            "name": fmt_info["name"],
            "extensions": fmt_info["extensions"],
            "selected_app": selected_app_name,
            "command": selected_cmd,
            "available_candidates": [c["name"] for c in matched_candidates if c["is_installed"]],
        }
        if verbose:
            cands_str = f" (可选候选: {', '.join(config['associations'][fmt_id]['available_candidates'])})" if config['associations'][fmt_id]['available_candidates'] else ""
            print(f"  • {fmt_info['name']} ({', '.join(fmt_info['extensions'])}): 匹配打开程序 -> [{selected_app_name}]{cands_str}")
    return config

def _merge_opener(existing: dict, defaults: dict) -> dict:
    """合并保留策略：人工仲裁的 selected_app/command 原样保留，仅回填缺失格式与缺失字段。"""
    merged = json.loads(json.dumps(existing, ensure_ascii=False))
    assoc = merged.setdefault("associations", {})
    if not isinstance(assoc, dict):
        assoc = merged["associations"] = {}
    for fmt_id, dflt in defaults.get("associations", {}).items():
        cur = assoc.get(fmt_id)
        if not isinstance(cur, dict):
            assoc[fmt_id] = json.loads(json.dumps(dflt, ensure_ascii=False))
            continue
        for key in ("name", "extensions", "selected_app", "command"):
            if not cur.get(key):
                cur[key] = dflt.get(key)
        if not isinstance(cur.get("available_candidates"), list):
            cur["available_candidates"] = dflt.get("available_candidates", [])
    merged.setdefault("version", defaults.get("version", "1.0.0"))
    merged.setdefault("platform", defaults.get("platform", sys.platform))
    return merged

def _write_opener_config(target_file: Path, config: dict, verbose: bool) -> None:
    target_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target_file, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        if verbose:
            print("✅ 已成功生成本机打开器配置: .data/file-opener.json")
    except Exception as e:
        if verbose:
            print(f"❌ 写入 .data/file-opener.json 失败: {e}", file=sys.stderr)

def init_file_opener(verbose: bool = True, force_rescan: bool = False, *, system_dir: Path | None = None) -> dict:
    """
    初始化或自愈本机文件打开器关联配置 (.data/file-opener.json)。

    合并保留策略（默认）：目标配置存在且合法时，仅回填缺失的格式与字段，
    人工仲裁的 selected_app/command 持久保留，内容无变化时不重写文件；
    全量重扫仅在显式 force_rescan=True（CLI: --force-rescan-opener）时执行；
    既有配置损坏（无法解析或结构非法）时自动重建自愈。
    """
    tools_dir = Path(__file__).resolve().parent
    system_dir = Path(system_dir) if system_dir is not None else tools_dir.parent
    data_dir = system_dir.parent / ".data"
    template_file = system_dir / "templates" / "file-opener.template.json"
    target_file = data_dir / "file-opener.json"

    if not template_file.exists():
        if verbose:
            print(f"⚠️ 未找到文件打开器模板: {template_file}", file=sys.stderr)
        return {}
    try:
        with open(template_file, "r", encoding="utf-8") as f:
            tpl_data = json.load(f)
    except Exception as e:
        if verbose:
            print(f"❌ 读取模板失败: {e}", file=sys.stderr)
        return {}

    cmd_key = _opener_cmd_key()

    if target_file.exists() and not force_rescan:
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = None
        if isinstance(existing, dict) and isinstance(existing.get("associations"), dict):
            defaults = _build_opener_defaults(tpl_data, detect_host_apps(), cmd_key, verbose)
            merged = _merge_opener(existing, defaults)
            if merged != existing:
                _write_opener_config(target_file, merged, verbose)
                if verbose:
                    print("✅ 文件打开器配置已合并补全（人工仲裁项已保留）")
            elif verbose:
                print("✅ 本机文件打开器配置已就绪 (.data/file-opener.json)")
            return merged
        if verbose:
            print("⚠️ 既有打开器配置损坏，自动重建自愈...")

    config = _build_opener_defaults(tpl_data, detect_host_apps(), cmd_key, verbose)
    _write_opener_config(target_file, config, verbose)
    return config

def get_open_command(file_path: str, root: Path | None = None) -> str:
    """
    根据文件路径或格式获取本机打开命令。
    优先读取 .data/file-opener.json 中的配置；缺失时优雅降级为系统默认命令。
    """
    path_obj = Path(file_path)
    if path_obj.is_dir() or not path_obj.suffix:
        if sys.platform == "darwin":
            return f'open "{file_path}"'
        elif sys.platform == "win32":
            return f'explorer "{file_path}"'
        else:
            return f'xdg-open "{file_path}"'

    ext = path_obj.suffix.lower()

    if root is None:
        tools_dir = Path(__file__).resolve().parent
        root = tools_dir.parent.parent

    config_file = root / ".data" / "file-opener.json"
    if config_file.is_file():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for _, assoc in cfg.get("associations", {}).items():
                if ext in assoc.get("extensions", []):
                    cmd = assoc.get("command", "")
                    if cmd:
                        return f'{cmd} "{file_path}"'
        except Exception:
            pass

    # 默认兜底
    if sys.platform == "darwin":
        return f'open "{file_path}"'
    elif sys.platform == "win32":
        return f'start "" "{file_path}"'
    else:
        return f'xdg-open "{file_path}"'

def print_windows_hints() -> None:
    print("""
⚠️  Windows 环境已知限制（Agent 决策参考）：
  • 私有仓库：git clone 无法认证时，立即引导用户在已登录的浏览器中下载 ZIP，
    不要继续尝试其他认证方式。
  • 浏览器会话复用：需要接管浏览器登录态时，须先确认 Chrome 以
    --remote-debugging-port=9222 启动且端口已监听（netstat -an | findstr 9222），
    再行操作；否则直接放弃该路径。
  • PowerShell 输出异常：若 PowerShell 返回空或报错，改用 cmd /c 执行等效命令，
    或用 Out-File 将输出写入临时文件再读取。
  • 目录可见性：安装后需隐藏 .system 和 .data 目录：
    attrib +h +s <工作区路径>\\.system
    attrib +h +s <工作区路径>\\.data
""")

if __name__ == "__main__":
    print("🚀 开始初始化/自愈工作区配置...")



    if setup_symlinks(verbose=True):
        token_ready = check_dashboard_token()
        if token_ready:
            print("✅ 看板 API Token 已就绪。")
        else:
            print("ℹ️ 看板尚未配置 Token（可直接对 Agent 说「配置看板 Token」或让 Agent 执行 init.py）。")
        render_instance_configs(verbose=True)
        force_rescan_opener = "--force-rescan-opener" in sys.argv
        init_file_opener(verbose=True, force_rescan=force_rescan_opener)
        print("\n✨ 工作区初始化与自愈完成！")
        if sys.platform == "win32":
            print_windows_hints()
    else:
        sys.exit(1)
