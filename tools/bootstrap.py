#!/usr/bin/env python3
"""
工作区根入口与系统配置初始化工具 (Bootstrap)
用于一键同步工作区根目录的 AGENTS.md / CLAUDE.md 入口文件，自动检测宿主机安装的应用程序，
生成/维护各类文件格式的默认打开器关联配置 (.entropaxis/data/templates/file-opener.json)，并自适应引导环境。
"""
import os
import sys
import json
import shutil
from pathlib import Path

try:
    from . import paths
except ImportError:
    # runpy.run_path()（install_windows.py 冻结后调用它的方式）不会把脚本自身目录
    # 加进 sys.path，跟直接 `python3 bootstrap.py` 的行为不同，这里补上避免
    # ModuleNotFoundError: No module named 'paths'。
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import paths

def render_instance_configs(
    verbose: bool = True,
    *,
    templates_dir: Path | None = None,
    data_dir: Path | None = None,
    ws_name: str | None = None,
) -> bool:
    """从 .entropaxis/templates/instance/*.template.{json,md,yaml} 首次渲染到 .entropaxis/data/。

    - 仅在 data/ 目标文件完全缺失时写入；存在即不动（避免覆盖用户已填内容）
    - 占位符 {{XXX}} 替换为工作区目录名兜底（无脑填充，明示待填）
    - 不阻断、不抛错；模板文件缺失时跳过
    - 写入用 tempfile + replace 实现原子替换
    - 三个路径/名称参数均可由测试覆写；生产调用全部传 None，从 __file__ 派生
    """
    import tempfile

    if templates_dir is None or data_dir is None:
        system_dir = paths.SYSTEM_DIR
        if templates_dir is None:
            templates_dir = system_dir / "templates"
        if data_dir is None:
            data_dir = paths.DATA_DIR
    (data_dir / "templates").mkdir(parents=True, exist_ok=True)

    if ws_name is None:
        ws_name = data_dir.parent.parent.name or "workspace"

    success = True
    rendered = 0


    # templates/instance/ 即实例模板白名单——目录按消费方划分，无需硬编码名单
    # （项目脚手架在 templates/project/，由 init_project / init_app 消费）
    data_templates = templates_dir / "instance"
    if not data_templates.is_dir():
        if verbose:
            print(f"ℹ️ 未找到实例模板目录 {data_templates}，跳过渲染")
        return True

    for tpl in sorted(data_templates.glob("*.template.json")):
        target_name = tpl.name.replace(".template.json", ".json")
        # 模板渲染产物一律落 .entropaxis/data/templates/，与源模板同名，路径即来源指针
        target = data_dir / "templates" / target_name
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
                print(f"✅ 已从模板渲染 .entropaxis/data/templates/{target_name}（首次，空 providers）")
        except Exception as exc:
            if verbose:
                print(f"❌ 渲染 {target_name} 失败: {exc}", file=sys.stderr)
            success = False
    # .md / .yaml 模板按文本渲染（YAML 保留注释，不经解析器重排）
    for tpl in sorted([*data_templates.glob("*.template.md"), *data_templates.glob("*.template.yaml")]):
        target_name = tpl.name.replace(".template", "", 1)
        target = data_dir / "templates" / target_name
        if target.exists():
            continue
        try:
            content = tpl.read_text(encoding="utf-8")
            # 占位符替换为工作区目录名兜底（明示待填）。共享资料层模板默认空列表：
            # 不写死任何具体目录名，也不拿探测结果冒充默认值。
            rendered_content = content.replace("{{ORG_FULL_NAME}}", f"{ws_name}（待填：组织完整名称）")
            rendered_content = rendered_content.replace("{{ORG_FORBIDDEN_ABBR}}", f"{ws_name}-abbr（待填：禁用缩写）")
            # 原子写入
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", delete=False, dir=str(data_dir / "templates"), prefix=f".{target_name}.tmp."
            ) as tmp:
                tmp.write(rendered_content)
                tmp_path = Path(tmp.name)
            tmp_path.replace(target)
            rendered += 1
            if verbose:
                print(f"✅ 已从模板渲染 .entropaxis/data/templates/{target_name}（首次，占位符已替换为目录名兜底）")
        except Exception as exc:
            if verbose:
                print(f"❌ 渲染 {target_name} 失败: {exc}", file=sys.stderr)
            success = False

    if verbose and rendered == 0:
        print("ℹ️ .entropaxis/data/ 实例模板无需渲染（目标文件已全部存在）")
    return success


def stamp_new_instances(verbose: bool = True) -> None:
    """给刚渲染出来的 `.entropaxis/data/` 实例文件补盖来源与写入策略标记。

    渲染与盖章分属两个工具，但对新工作区来说是同一件事的两半：不在初始化里闭合，
    新人第一次体检就会看到几条"实例文件缺来源标记"建议，而这批文件恰恰是初始化
    自己刚写下的。工具不可用时静默跳过，不阻断初始化。
    """
    tools_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(tools_dir))
    try:
        import stamp_data_provenance as provenance
    except Exception as exc:  # noqa: BLE001 - 补盖失败不应阻断初始化
        if verbose:
            print(f"ℹ️ 来源标记工具不可用，跳过补盖：{exc}")
        return
    finally:
        if str(tools_dir) in sys.path:
            sys.path.remove(str(tools_dir))
    stamped = sum(1 for p in provenance.target_files() if provenance.stamp(p))
    if verbose and stamped:
        print(f"✅ 已为 {stamped} 个 .entropaxis/data/ 实例文件补盖来源标记")


def sync_entrypoints(verbose: bool = True) -> bool:
    """
    将 .entropaxis/entrypoints 下的根入口真源物理同步至工作区根目录。
    为避免 Synology Drive / 云同步网盘在跨平台同步时对软链接产生 Conflict 冲突，
    采用幂等文件复制（shutil.copy2）作为标准同步策略。
    """
    tools_dir = Path(__file__).resolve().parent
    system_dir = tools_dir.parent
    ws_root = system_dir.parent
    entrypoints = system_dir / "entrypoints"

    if not entrypoints.exists():
        if verbose:
            print(f"❌ 错误: 未找到根入口源目录 {entrypoints}", file=sys.stderr)
        return False

    success = True
    for filename in ["AGENTS.md", "CLAUDE.md"]:
        src = entrypoints / filename
        dst = ws_root / filename

        if not src.exists():
            continue

        try:
            # 如果目标是软链接或已存在文件，直接清理后复制，消除网盘软链接冲突
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            shutil.copy2(src, dst)
            if verbose:
                print(f"✅ 入口文件同步就绪: {filename} <- .entropaxis/entrypoints/{filename}")
        except Exception as e:
            if verbose:
                print(f"❌ 入口同步失败 ({filename}): {e}", file=sys.stderr)
            success = False

    return success

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
    return config


def _report_opener(config: dict) -> None:
    """打印**实际生效**的打开器映射，并标注每项来源。

    此前打印的是本机检测结果而非配置生效值——用户手工注册的程序（模板候选之外，
    如自建 CLI）不会出现在检测结果里，于是每次运行都像被重置了一样，
    进而诱导 Agent 去"修正"配置，把人工仲裁值真的覆盖掉。
    """
    print("📖 当前生效的文件打开器映射（.entropaxis/data/templates/file-opener.json）：")
    for info in config.get("associations", {}).values():
        mark = "🔒人工仲裁" if info.get("arbitrated") else "自动匹配"
        exts = ", ".join(info.get("extensions", []))
        print(f"  • {info.get('name', '?')} ({exts}): [{info.get('selected_app', '?')}] {mark}")

def _merge_opener(existing: dict, defaults: dict) -> dict:
    """合并保留策略：人工仲裁的 selected_app/command 原样保留，仅回填缺失格式与缺失字段。

    存量配置的 command 与本机检测默认值不一致，说明有人**刻意**改过（手工注册了模板候选
    之外的程序），据此打上 `arbitrated` 标记。该标记既让人一眼看出哪些是人工决定，
    也让后续任何 Agent 在写这个文件前看到"此项不得覆盖"。
    """
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
        if cur.get("arbitrated") is None and cur.get("command") != dflt.get("command"):
            cur["arbitrated"] = True
    merged.setdefault("version", defaults.get("version", "1.0.0"))
    merged.setdefault("platform", defaults.get("platform", sys.platform))
    return merged

def _write_opener_config(target_file: Path, config: dict, verbose: bool) -> None:
    target_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target_file, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        if verbose:
            print("✅ 已成功生成本机打开器配置: .entropaxis/data/templates/file-opener.json")
    except Exception as e:
        if verbose:
            print(f"❌ 写入 .entropaxis/data/templates/file-opener.json 失败: {e}", file=sys.stderr)

def init_file_opener(verbose: bool = True, force_rescan: bool = False, *, system_dir: Path | None = None) -> dict:
    """
    初始化或自愈本机文件打开器关联配置 (.entropaxis/data/templates/file-opener.json)。

    合并保留策略（默认）：目标配置存在且合法时，仅回填缺失的格式与字段，
    人工仲裁的 selected_app/command 持久保留，内容无变化时不重写文件；
    全量重扫仅在显式 force_rescan=True（CLI: --force-rescan-opener）时执行；
    既有配置损坏（无法解析或结构非法）时自动重建自愈。
    """
    tools_dir = Path(__file__).resolve().parent
    system_dir = Path(system_dir) if system_dir is not None else tools_dir.parent
    data_dir = system_dir / "data"
    template_file = system_dir / "templates" / "instance" / "file-opener.template.json"
    target_file = data_dir / "templates" / "file-opener.json"

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
            # 检测过程不打印：那是候选发现结果，不是生效配置，混淆二者会诱发误"修复"
            defaults = _build_opener_defaults(tpl_data, detect_host_apps(), cmd_key, verbose=False)
            merged = _merge_opener(existing, defaults)
            if merged != existing:
                _write_opener_config(target_file, merged, verbose)
            if verbose:
                _report_opener(merged)
            return merged
        if verbose:
            print("⚠️ 既有打开器配置损坏（无法解析或结构非法），自动重建自愈...")

    config = _build_opener_defaults(tpl_data, detect_host_apps(), cmd_key, verbose)
    _write_opener_config(target_file, config, verbose)
    if verbose:
        _report_opener(config)
    return config


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
  • 目录可见性：安装后需隐藏 .entropaxis 目录（data/ 已在其内，一并隐藏）：
    attrib +h +s <工作区路径>\\.entropaxis
""")

def print_legacy_layout_hint() -> None:
    """检测工作区根目录下的旧布局残留（.system/ 或 .data/），只提示不自动迁移。

    两种成因（旧版本升级遗留 / 某写入方未按契约在根目录误建）现场无法区分，
    因此文案不区分来源，只给出统一的人工迁移指引；全量搬迁须由用户显式指令
    触发，不得由初始化顺带执行（见《控制面布局》「.data/ 写入规约」硬约束 3）。
    """
    found = paths.legacy_layout_present()
    if not found:
        return
    print("\n⚠️ 检测到工作区根目录下存在旧布局残留：" + "、".join(found))
    print("👉 请人工确认后手动迁移，不会自动执行：")
    if paths.LEGACY_SYSTEM_DIRNAME in found:
        print(f"   mv {paths.LEGACY_SYSTEM_DIRNAME} {paths.SYSTEM_DIRNAME}")
    if paths.LEGACY_DATA_DIRNAME in found:
        print(f"   mv {paths.LEGACY_DATA_DIRNAME} {paths.SYSTEM_DIRNAME}/data")


if __name__ == "__main__":
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    force_rescan_opener = "--force-rescan-opener" in sys.argv

    if verbose:
        print("🚀 开始初始化/自愈工作区配置...")

    print_legacy_layout_hint()

    if sync_entrypoints(verbose=verbose):
        init_file_opener(verbose=verbose, force_rescan=force_rescan_opener)
        render_instance_configs(verbose=verbose)
        stamp_new_instances(verbose=verbose)
        print("🎉 工作区初始化与自愈完成（入口已同步，实例配置就绪；角色默认内置 Subagent 兜底）。")
        print("💡 进阶自定义：说「自定义角色」绑定多模型（roles.yaml） / 「配置打开方式」 / registry.md 登记别名 / workspace-config.yaml 组织口径")
        if sys.platform == "win32" and verbose:
            print_windows_hints()
    else:
        sys.exit(1)
