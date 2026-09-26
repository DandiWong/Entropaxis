#!/usr/bin/env python3
"""角色模态与外部 Agent 交互配置工具 (Agent Role Setup & Discovery Tool).

用于检测宿主机环境中已安装的各类 Agent CLI（如 omp、claude、codex、gemini、cursor、aider 等），
提供 7 个标准角色的命令预设，并把角色承载写入 .entropaxis/data/templates/roles.yaml
（契约 schemas/roles_config.schema.json；命令以结构化 argv 存于 command_profiles）。

用法:
  # 1. 扫描当前环境已安装的 Agent CLI
  python3 .entropaxis/tools/setup_agents.py --scan
  python3 .entropaxis/tools/setup_agents.py --scan --json

  # 2. 校验 roles.yaml 的 schema 与各角色承载链
  python3 .entropaxis/tools/setup_agents.py --verify

  # 3. 交互式向导配置各个角色的承载 CLI 与启动命令
  python3 .entropaxis/tools/setup_agents.py

  # 4. 一键为所有角色批量应用指定 Agent 的推荐预设
  python3 .entropaxis/tools/setup_agents.py --apply-preset omp
  python3 .entropaxis/tools/setup_agents.py --apply-preset subagent

  # 5. 精确设置指定角色的 CLI 和启动命令
  python3 .entropaxis/tools/setup_agents.py --set-role Reviewer omp "omp --model a/b || claude -p {PROMPT}"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

try:
    from . import paths
except ImportError:
    import paths

ROOT = paths.WORKSPACE_ROOT
DEFAULT_CONFIG_PATH = paths.DATA_DIR / "templates" / "roles.yaml"
TEMPLATE_PATH = paths.SYSTEM_DIR / "templates" / "instance" / "roles.template.yaml"
PROMPT = "{PROMPT}"
PROMPT_FLAG_FAMILIES = {"omp", "claude", "codex"}  # 缺占位符时可安全补 `-p {PROMPT}` 的 CLI 族
SHELL_METACHARS = set(';&|`$><\n')
ROLE_NAME_RE = re.compile(r"^[A-Z][A-Za-z]*$")

# 7 个标准角色（Manager 由当前会话承担，不在此配置）
STANDARD_ROLES: dict[str, str] = {
    "Architecture": "顶层架构/技术选型/方案设计",
    "Researcher": "调研（强制联网）/文献综述/竞品查新",
    "Designer": "视觉美学/UI交互原型/排版呈现",
    "Builder": "方案实施/核心编码/重构",
    "Reviewer": "方案审计/对抗评审/架构合规",
    "Maintainer": "全流程验收/守门落盘/证据核验",
    "Reporter": "汇报材料提炼/交付总结",
}


class AgentInfo(NamedTuple):
    id: str
    name: str
    cli: str
    version_cmd: list[str]
    description: str
    presets: dict[str, str]


# 常见 Agent 及其针对认知模态的经过验证的标准启动命令预设
KNOWN_AGENTS: list[AgentInfo] = [
    AgentInfo(
        id="subagent",
        name="内置 Subagent (当前 Agent 内置机制)",
        cli="subagent",
        version_cmd=[],
        description="无需外部 CLI，直接使用当前 Agent 会话的子代理机制（自闭环默认选项）",
        presets={
            "Architecture": "内置 Subagent 机制 (auto)",
            "Reviewer": "内置 Subagent 机制 (auto)",
            "Researcher": "内置 Subagent 机制 (auto)",
            "Builder": "内置 Subagent 机制 (auto)",
            "Designer": "内置 Subagent 机制 (auto)",
            "Maintainer": "内置 Subagent 机制 (auto)",
            "Reporter": "内置 Subagent 机制 (auto)",
        },
    ),
    AgentInfo(
        id="omp",
        name="Oh My Pi (omp)",
        cli="omp",
        version_cmd=["omp", "--version"],
        description="多模型编码与代理工具，支持 OpenAI/Anthropic/Gemini 独立进程调用",
        presets={
            "Architecture": "omp --model openai-codex/gpt-5.6-terra",
            "Reviewer": "omp --model openai-codex/gpt-5.6-terra",
            "Researcher": "omp --model google-antigravity/gemini-3.8-flash || omp --model minimax-coding-cn/MiniMax-M3",
            "Builder": "omp --model zhipu-coding-plan/glm-5.3 || claude --model sonnet-5",
            "Designer": "claude --model sonnet-5",
            "Maintainer": "claude --model opus-5",
            "Reporter": "omp --model google-antigravity/gemini-3.8-flash || omp --model minimax-coding-cn/MiniMax-M3",
        },
    ),
    AgentInfo(
        id="claude",
        name="Claude Code (claude)",
        cli="claude",
        version_cmd=["claude", "--version"],
        description="Anthropic 官方终端 Agent 工具，擅长深度推理与架构设计",
        presets={
            "Architecture": "claude --model opus-5",
            "Reviewer": "claude -p \"{prompt}\"",
            "Researcher": "claude -p \"{prompt}\"",
            "Builder": "claude",
            "Designer": "claude --model sonnet-5",
            "Maintainer": "claude --model opus-5",
            "Reporter": "claude -p \"{prompt}\"",
        },
    ),
    AgentInfo(
        id="codex",
        name="OpenAI Codex CLI (codex)",
        cli="codex",
        version_cmd=["codex", "--version"],
        description="OpenAI 代码生成与执行 CLI",
        presets={
            "Reviewer": "codex exec \"{prompt}\"",
            "Builder": "codex exec \"{prompt}\"",
        },
    ),
    AgentInfo(
        id="gemini",
        name="Gemini CLI (gemini)",
        cli="gemini",
        version_cmd=["gemini", "--version"],
        description="Google Gemini 命令行工具，大上下文检索能力突出",
        presets={
            "Researcher": "gemini \"{prompt}\"",
            "Reviewer": "gemini \"{prompt}\"",
        },
    ),
    AgentInfo(
        id="cursor",
        name="Cursor Agent / CLI (cursor)",
        cli="cursor",
        version_cmd=["cursor", "--version"],
        description="Cursor IDE 终端代理集成",
        presets={
            "Builder": "cursor",
        },
    ),
    AgentInfo(
        id="aider",
        name="Aider (aider)",
        cli="aider",
        version_cmd=["aider", "--version"],
        description="AI 协同编程 CLI",
        presets={
            "Builder": "aider",
            "Reviewer": "aider --lint-only",
        },
    ),
    AgentInfo(
        id="ollama",
        name="Ollama (ollama)",
        cli="ollama",
        version_cmd=["ollama", "--version"],
        description="本地大语言模型运行工具",
        presets={
            "Researcher": "ollama run qwen2.5:32b",
        },
    ),
]


def detect_installed_agents() -> list[dict[str, Any]]:
    """扫描系统环境检测已安装的 Agent CLI 及其版本信息。"""
    results: list[dict[str, Any]] = []

    for agent in KNOWN_AGENTS:
        if agent.id == "subagent":
            results.append({
                "id": agent.id,
                "name": agent.name,
                "cli": agent.cli,
                "installed": True,
                "version": "built-in",
                "path": "(builtin)",
                "description": agent.description,
                "presets": agent.presets,
            })
            continue

        cli_path = shutil.which(agent.cli)
        if not cli_path:
            results.append({
                "id": agent.id,
                "name": agent.name,
                "cli": agent.cli,
                "installed": False,
                "version": None,
                "path": None,
                "description": agent.description,
                "presets": agent.presets,
            })
            continue

        version = "detected"
        if agent.version_cmd:
            try:
                proc = subprocess.run(
                    agent.version_cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                output = (proc.stdout or proc.stderr or "").strip()
                if output:
                    version = output.splitlines()[0][:60]
            except Exception:
                version = "unknown-version"

        results.append({
            "id": agent.id,
            "name": agent.name,
            "cli": agent.cli,
            "installed": True,
            "version": version,
            "path": cli_path,
            "description": agent.description,
            "presets": agent.presets,
        })

    return results


def parse_role_table(content: str) -> dict[str, dict[str, str]]:
    """旧布局只读兼容：从 workspace-config.md 解析角色 Markdown 表（新布局见 roles.yaml）。

    返回结构: { "Reviewer": {"duty": "...", "cli": "...", "cmd": "..."}, ... }
    """
    roles: dict[str, dict[str, str]] = {}
    in_role_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if "## 角色模态外置 CLI 与模型声明" in line or "## 审计角色外置 CLI 声明" in line:
            in_role_section = True
            continue
        if in_role_section and stripped.startswith("## "):
            break
        if not in_role_section or not stripped.startswith("|") or not stripped.endswith("|"):
            continue

        raw_cols = re.split(r"(?<!\\)\|", stripped)[1:-1]
        cols = [c.strip().replace(r"\|", "|") for c in raw_cols]
        if not cols or cols[0] in ("角色", "角色模态") or set(cols[0]) <= set("-: "):
            continue

        role_raw = cols[0]
        role_key = role_raw.split("（")[0].split("(")[0].strip()

        if len(cols) >= 4:
            duty, cli, cmd = cols[1], cols[2], cols[3]
        elif len(cols) == 3:
            duty = STANDARD_ROLES.get(role_key, "业务协作")
            cli, cmd = cols[1], cols[2]
        elif len(cols) == 2:
            duty = STANDARD_ROLES.get(role_key, "业务协作")
            cli, cmd = cols[1], "内置 Subagent 机制 (auto)"
        else:
            continue

        roles[role_key] = {
            "duty": duty,
            "cli": cli,
            "cmd": cmd.strip("`"),
        }

    return roles


class ConfigError(ValueError):
    """命令无法安全转成结构化 argv（缺占位符 / 含 shell 元字符 / 角色名非法）。"""


def _yaml():
    try:
        import yaml  # noqa: PLC0415 - 与 dispatch_role.py 同一依赖，roles.yaml 读写均经它
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("❌ 需要 PyYAML 读写 roles.yaml\n👉 pip install pyyaml") from exc
    return yaml


def load_config(config_path: Path) -> dict[str, Any]:
    """读 roles.yaml；缺失时以模板为底（仅内存，不落盘）。"""
    src = config_path if config_path.exists() else TEMPLATE_PATH
    data = _yaml().safe_load(src.read_text(encoding="utf-8")) if src.exists() else None
    data = data or {}
    data.setdefault("default_dispatch_mode", "strict")
    data["roles"] = data.get("roles") or {}
    data["command_profiles"] = data.get("command_profiles") or {}
    data.setdefault("dispatch_authorizations", [])
    return data


def save_config(config_path: Path, data: dict[str, Any]) -> None:
    """校验通过才原子写入；失败保留原文件。"""
    errs = schema_errors(data)
    if errs:
        raise ConfigError("roles.yaml 未通过 schema：" + "；".join(errs[:3]))
    text = _yaml().safe_dump(data, allow_unicode=True, sort_keys=False, width=1000)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=config_path.parent, delete=False) as tf:
        tf.write(text)
    os.replace(tf.name, config_path)


def schema_errors(data: dict[str, Any]) -> list[str]:
    try:
        from . import validate_schema as vs  # noqa: PLC0415
    except ImportError:
        import validate_schema as vs  # noqa: PLC0415
    return vs.validate(data, vs.load_schema("roles_config"))


def profile_chain(data: dict[str, Any], name: str | None) -> list[tuple[str, dict[str, Any]]]:
    """沿 fallback_profile 展开，链深 ≤3、遇环即停（与 dispatch_role 同口径）。"""
    chain: list[tuple[str, dict[str, Any]]] = []
    profiles = data.get("command_profiles") or {}
    while name and name in profiles and len(chain) < 3 and all(n != name for n, _ in chain):
        chain.append((name, profiles[name]))
        name = profiles[name].get("fallback_profile")
    return chain


def command_to_argvs(cmd: str) -> list[list[str]]:
    """「a || b」自由命令 → 结构化 argv 链。{prompt} 统一为 {PROMPT}；
    已知 CLI 族缺占位符时补 `-p {PROMPT}`，其余缺占位符即拒绝（无法注入任务文本）。"""
    argvs = []
    for cand in (c.strip().strip("`") for c in cmd.split("||")):
        if not cand:
            continue
        try:
            tokens = [PROMPT if t.lower() == "{prompt}" else t for t in shlex.split(cand)]
        except ValueError as exc:
            raise ConfigError(f"命令无法分词: {cand!r}（{exc}）") from exc
        bad = [t for t in tokens if t != PROMPT and set(t) & SHELL_METACHARS]
        if bad:
            raise ConfigError(f"argv 含 shell 元字符 {bad}；备选请用 `||` 分隔整条命令")
        if PROMPT not in tokens:
            if Path(tokens[0]).name not in PROMPT_FLAG_FAMILIES:
                raise ConfigError(f"`{cand}` 缺 {{PROMPT}} 占位符，调度器无法注入任务文本；请在命令中写明位置")
            tokens += ["-p", PROMPT]
        argvs.append(tokens)
    if not argvs:
        raise ConfigError("命令为空")
    if len(argvs) > 3:
        raise ConfigError("备选链深须 ≤3")
    return argvs


def set_role(data: dict[str, Any], role: str, cli: str, cmd: str) -> None:
    """改写单个角色：subagent → profile 置空；否则重建 `<角色>-primary/-fallback/-fallback-2` 链。"""
    if not ROLE_NAME_RE.match(role):
        raise ConfigError(f"角色名须为英文 PascalCase（如 Reviewer、DataSteward），实得 {role!r}")
    roles, profiles = data["roles"], data["command_profiles"]
    entry = roles.setdefault(role, {})
    entry.setdefault("duty", STANDARD_ROLES.get(role, "业务协作"))
    old = [n for n, _ in profile_chain(data, entry.get("profile"))]
    if cli == "subagent" or "内置" in cmd or cmd.strip() in ("", "auto", "subagent"):
        entry["profile"] = None
        new: list[str] = []
    else:
        slug = role.lower()
        names = [f"{slug}-primary", f"{slug}-fallback", f"{slug}-fallback-2"]
        argvs = command_to_argvs(cmd)
        new = names[: len(argvs)]
        for i, (name, argv) in enumerate(zip(new, argvs)):
            prof = {"argv": argv, "timeout_s": (profiles.get(name) or {}).get("timeout_s", 900)}
            if i + 1 < len(new):
                prof["fallback_profile"] = new[i + 1]
            profiles[name] = prof
        entry["profile"] = new[0]
    # 清掉本角色旧链里不再被任何角色/备选引用的 profile，避免孤儿配置
    dropped = set(old) - set(new)
    referenced = {r.get("profile") for r in roles.values()} | {
        p.get("fallback_profile") for n, p in profiles.items() if n not in dropped}
    for name in old:
        if name not in new and name not in referenced:
            profiles.pop(name, None)


def role_view(data: dict[str, Any]) -> dict[str, dict[str, str]]:
    """人读视图：{角色: {duty, cli, cmd}}，cmd 以「 || 」连接备选链。"""
    view = {}
    for role, entry in (data.get("roles") or {}).items():
        chain = profile_chain(data, (entry or {}).get("profile"))
        view[role] = {
            "duty": (entry or {}).get("duty", ""),
            "profile": (entry or {}).get("profile") or "",
            "cli": Path(chain[0][1]["argv"][0]).name if chain else "subagent",
            "cmd": " || ".join(shlex.join(p["argv"]) for _, p in chain) if chain else "内置 Subagent 机制 (auto)",
        }
    return view


def verify_roles(config_path: Path) -> list[dict[str, Any]]:
    """校验 roles.yaml 的 schema 与各角色承载链的可执行状态。"""
    if not config_path.exists():
        return [{"role": "ALL", "status": "missing_config", "msg": f"配置文件不存在: {config_path}（运行 bootstrap.py 初始化）"}]
    data = load_config(config_path)
    errs = schema_errors(data)
    if errs:
        return [{"role": "ALL", "status": "schema_error", "msg": "；".join(errs)}]
    reports: list[dict[str, Any]] = []
    for role, info in role_view(data).items():
        base = {"role": role, "cli": info["cli"], "cmd": info["cmd"], "profile": info["profile"]}
        if not info["profile"]:
            reports.append({**base, "status": "subagent_default", "msg": "✅ 内置 Subagent 承载（默认）。"})
            continue
        chain = profile_chain(data, info["profile"])
        if not chain:
            reports.append({**base, "status": "profile_missing", "msg": f"❌ profile `{info['profile']}` 未在 command_profiles 中定义。"})
            continue
        found = [n for n, p in chain if shutil.which(p["argv"][0])]
        missing = [n for n, p in chain if n not in found]
        if found:
            msg = f"✅ 外置就绪: {' → '.join(found)}" + (f"；⚠️ 未找到: {', '.join(missing)}" if missing else "")
            reports.append({**base, "status": "external_ok", "msg": msg})
        else:
            reports.append({**base, "status": "external_missing",
                            "msg": "⚠️ 链上 CLI 均不在 PATH；按 rules/角色协作.md 失败矩阵裁决（硬门禁角色将 blocked）。"})
    return reports


def interactive_wizard(config_path: Path) -> None:
    """终端交互式配置向导（Agent 会话内请走 --scan/--set-role/--verify 问答流程）。"""
    detected = detect_installed_agents()
    installed = {item["id"] for item in detected if item["installed"]}
    data = load_config(config_path)
    view = role_view(data)
    for role_name, duty_desc in STANDARD_ROLES.items():
        curr = view.get(role_name, {"cmd": "内置 Subagent 机制 (auto)"})
        print(f"\n【{role_name}】{duty_desc}\n  当前: {curr['cmd']}")
        options: list[tuple[str, str, str]] = [("内置 Subagent", "subagent", "")]
        for agent in KNOWN_AGENTS:
            if agent.id != "subagent" and agent.id in installed:
                preset = agent.presets.get(role_name, f"{agent.cli} -p {{prompt}}")
                options.append((f"{agent.name}: {preset}", agent.cli, preset))
        options.append(("自定义命令", "custom", ""))
        for idx, (label, _, _) in enumerate(options, 1):
            print(f"  [{idx}] {label}")
        try:
            choice = input(f"  选择 [1-{len(options)}]，回车保持: ").strip()
            if not choice:
                continue
            label, cli, cmd = options[int(choice) - 1]
            if cli == "custom":
                cmd = input("  启动命令（含 {PROMPT}，备选用 || 分隔）: ").strip()
                cli = cmd.split()[0] if cmd else "subagent"
            set_role(data, role_name, cli, cmd)
        except (EOFError, KeyboardInterrupt):
            print("\n已取消，未写入。")
            return
        except (ValueError, IndexError) as exc:
            print(f"  ⚠️ {exc}，保持当前配置。")
    save_config(config_path, data)
    print(f"\n✅ 已写入 {config_path}")
    for rep in verify_roles(config_path):
        print(f"  • {rep['role']}: {rep['msg']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Entropaxis 角色承载配置工具（data/templates/roles.yaml）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--scan", "-s", action="store_true", help="扫描宿主机已安装的 Agent CLI")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出扫描或校验结果")
    parser.add_argument("--verify", "-v", action="store_true", help="校验 roles.yaml 的 schema 与各角色承载链")
    parser.add_argument("--apply-preset", metavar="AGENT", help="一键为全部标准角色应用指定 Agent 的预设 (如 omp/subagent/claude)")
    parser.add_argument("--set-role", nargs=3, metavar=("ROLE", "CLI", "CMD"),
                        help="设定角色承载：CMD 可用 || 串备选（≤3），CLI 为 subagent 时改回内置承载")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="roles.yaml 路径")
    args = parser.parse_args()

    if args.scan:
        detected = detect_installed_agents()
        if args.json:
            print(json.dumps(detected, ensure_ascii=False, separators=(",", ":")))
        else:
            for item in detected:
                where = item["path"] or "未安装"
                print(f"{'✅' if item['installed'] else '—'} {item['id']}: {where}")
        return 0

    if args.verify:
        reports = verify_roles(args.config)
        if args.json:
            print(json.dumps(reports, ensure_ascii=False, separators=(",", ":")))
        else:
            for r in reports:
                print(f"{r.get('role')}: {r.get('msg')}")
        return 1 if any(r["status"] in ("schema_error", "profile_missing", "missing_config") for r in reports) else 0

    if args.apply_preset or args.set_role:
        data = load_config(args.config)
        try:
            if args.set_role:
                role_name, cli_val, cmd_val = args.set_role
                set_role(data, role_name, cli_val, cmd_val)
                changed = [role_name]
            else:
                agent_id = args.apply_preset.lower().strip()
                matched = next((a for a in KNOWN_AGENTS if agent_id in (a.id, a.cli)), None)
                if not matched:
                    print(f"❌ 未知 Agent 预设: {args.apply_preset}\n👉 可选: {', '.join(a.id for a in KNOWN_AGENTS)}",
                          file=sys.stderr)
                    return 1
                for role_name in STANDARD_ROLES:
                    set_role(data, role_name, matched.cli,
                             matched.presets.get(role_name, f"{matched.cli} -p {{prompt}}"))
                changed = list(STANDARD_ROLES)
            save_config(args.config, data)
        except ConfigError as exc:
            print(f"❌ {exc}\n👉 未写入；修正命令后重试。", file=sys.stderr)
            return 1
        view = role_view(data)
        for role_name in changed:
            print(f"✅ {role_name} → {view[role_name]['cmd']}")
        return 0

    if sys.stdin.isatty():
        interactive_wizard(args.config)
        return 0
    for r in verify_roles(args.config):
        print(f"{r.get('role')}: {r.get('msg')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
