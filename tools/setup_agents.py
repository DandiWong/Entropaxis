#!/usr/bin/env python3
"""角色模态与外部 Agent 交互配置工具 (Agent Role Setup & Discovery Tool).

用于检测宿主机环境中已安装的各类 Agent CLI（如 omp、claude、codex、gemini、cursor、aider 等），
提供针对 5 大认知模态（Reviewer、Researcher、Builder、Designer、Maintainer）的最佳命令预设，
并通过交互式或命令行方式辅助用户配置 .entropaxis/data/templates/workspace-config.md 中的角色承载 CLI。

用法:
  # 1. 扫描当前环境已安装的 Agent CLI
  python3 .entropaxis/tools/setup_agents.py --scan
  python3 .entropaxis/tools/setup_agents.py --scan --json

  # 2. 校验当前 workspace-config.md 中已配置角色的可用性与降级状态
  python3 .entropaxis/tools/setup_agents.py --verify

  # 3. 交互式向导配置各个角色的承载 CLI 与启动命令
  python3 .entropaxis/tools/setup_agents.py

  # 4. 一键为所有角色批量应用指定 Agent 的推荐预设
  python3 .entropaxis/tools/setup_agents.py --apply-preset omp
  python3 .entropaxis/tools/setup_agents.py --apply-preset subagent

  # 5. 精确设置指定角色的 CLI 和启动命令
  python3 .entropaxis/tools/setup_agents.py --set-role Reviewer omp "omp --model openai-codex/gpt-5.6-terra"
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
DEFAULT_CONFIG_PATH = paths.DATA_DIR / "templates" / "workspace-config.md"

# 7 大标准认知模态及其职责定义
STANDARD_ROLES: dict[str, str] = {
    "Architecture": "顶层架构/技术选型/方案设计",
    "Researcher": "调研（强制联网）/文献综述/竞品查新",
    "Designer": "视觉美学/UI交互原型/排版呈现",
    "Builder": "方案实施/核心编码/重构",
    "Reviewer": "方案审计/对抗评审/架构合规",
    "Maintainer": "全流程验收/守门落盘/证据核验",
    "Reporter": "汇报总结/周报双周报/交付归档",
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
    """从 workspace-config.md 内容中解析角色模态声明表格。

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


def render_role_section(roles_data: dict[str, dict[str, str]]) -> str:
    """生成标准角色模态 Markdown 表格段落。"""
    lines = [
        "## 角色模态外置 CLI 与模型声明",
        "",
        "当认知模态外置为独立 CLI/Agent 进程承担时（跨 CLI 协作，见 `.entropaxis/rules/角色协作.md`），本机生效的启动命令。未配置或指定为 `subagent` 时默认使用当前 Agent 的内置 Subagent 机制：",
        "",
        "| 角色模态 | 职责定位 | 承载 CLI | 启动命令 |",
        "|---|---|---|---|",
    ]

    # 按标准 5 大角色顺序输出，若有自定义角色追加在后
    ordered_keys = list(STANDARD_ROLES.keys())
    for k in roles_data:
        if k not in ordered_keys:
            ordered_keys.append(k)

    for role in ordered_keys:
        info = roles_data.get(role, {})
        duty = info.get("duty") or STANDARD_ROLES.get(role, "业务协作")
        cli = info.get("cli") or "subagent"
        cmd = info.get("cmd") or "内置 Subagent 机制 (auto)"
        if cli != "subagent" and not cmd.startswith("`") and not cmd.startswith("内置"):
            cmd_escaped = cmd.replace("|", r"\|")
            cmd_display = f"`{cmd_escaped}`"
        else:
            cmd_display = cmd.replace("|", r"\|")
        lines.append(f"| {role} | {duty} | {cli} | {cmd_display} |")

    return "\n".join(lines)


def update_workspace_config(config_path: Path, roles_data: dict[str, dict[str, str]]) -> bool:
    """原子更新 workspace-config.md 中的角色模态声明表格，保留前置 Front Matter 和其他章节。"""
    if not config_path.exists():
        print(f"❌ 配置文件不存在: {config_path}", file=sys.stderr)
        print("👉 修复建议: 请先运行 `python3 .entropaxis/tools/bootstrap.py` 初始化基础模板。", file=sys.stderr)
        return False

    content = config_path.read_text(encoding="utf-8")
    new_section = render_role_section(roles_data)

    # 匹配角色声明章节（从 ## 角色模态外置 CLI 与模型声明 或 ## 审计角色外置 CLI 声明 到下一个 ## 或文件末尾）
    section_pattern = re.compile(
        r"(##\s*(?:角色模态外置\s*CLI\s*与模型声明|审计角色外置\s*CLI\s*声明).*?)(?=\n##\s|\Z)",
        re.DOTALL,
    )

    if section_pattern.search(content):
        new_content = section_pattern.sub(new_section, content)
    else:
        new_content = content.rstrip() + "\n\n" + new_section + "\n"

    # 原子写入
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=config_path.parent, delete=False) as tf:
        tf.write(new_content)
        tmp_name = tf.name

    os.replace(tmp_name, config_path)
    return True


def verify_roles(config_path: Path) -> list[dict[str, Any]]:
    """校验当前 workspace-config.md 中各角色的 CLI 配置与实际可执行状态。"""
    if not config_path.exists():
        return [{"role": "ALL", "status": "missing_config", "msg": f"配置文件不存在: {config_path}"}]

    content = config_path.read_text(encoding="utf-8")
    roles = parse_role_table(content)
    if not roles:
        return [{"role": "ALL", "status": "empty", "msg": "未在配置文件中解析到任何角色声明。"}]

    reports: list[dict[str, Any]] = []
    for role, info in roles.items():
        cli = info.get("cli", "").strip()
        cmd = info.get("cmd", "").strip()

        if not cli or cli == "subagent" or "内置" in cmd:
            reports.append({
                "role": role,
                "cli": cli or "subagent",
                "cmd": cmd or "内置 Subagent 机制 (auto)",
                "status": "subagent_default",
                "msg": "✅ 使用当前 Agent 内置 Subagent 机制（系统默认，开箱即用）。",
            })
            continue
        # 支持多候选命令降级链（通过 || 分隔）
        sub_cmds = [c.strip().strip("`") for c in cmd.split("||")]
        valid_cmds = []
        missing_cmds = []

        for sc in sub_cmds:
            if not sc:
                continue
            tokens = sc.split()
            prog = tokens[0].strip("`'\"")
            p_path = shutil.which(prog)
            if p_path:
                valid_cmds.append((sc, prog, p_path))
            else:
                missing_cmds.append((sc, prog))

        if valid_cmds:
            primary = valid_cmds[0]
            fallbacks = valid_cmds[1:]
            fallback_desc = f"（配置了 {len(fallbacks)} 个备选降级）" if fallbacks else ""
            msg = f"✅ 外置进程就绪: 主选 `{primary[1]}` ({primary[2]}) {fallback_desc}。"
            if missing_cmds:
                msg += f" ⚠️ 部分备选 CLI 未找到: {', '.join(m[1] for m in missing_cmds)}。"
            reports.append({
                "role": role,
                "cli": cli,
                "cmd": cmd,
                "status": "external_ok",
                "msg": msg,
            })
        else:
            reports.append({
                "role": role,
                "cli": cli,
                "cmd": cmd,
                "status": "external_missing",
                "msg": f"⚠️ 外置 CLI 均未在 PATH 中找到！运行时将自动优雅降级为内置 Subagent 机制。",
            })
    return reports


def interactive_wizard(config_path: Path) -> None:
    """终端交互式配置向导。"""
    print("=" * 65)
    print("🤖 Entropaxis 角色模态与外部 Agent 交互配置向导")
    print("=" * 65)

    detected = detect_installed_agents()
    installed_map = {item["id"]: item for item in detected if item["installed"]}

    print("\n🔍 宿主机 Agent CLI 检测结果:")
    for item in detected:
        if item["installed"]:
            print(f"  • [已安装] {item['name']} ➔ 版本/路径: {item['version']}")
        else:
            print(f"  • [未安装] {item['name']} (CLI: `{item['cli']}`)")

    content = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    current_roles = parse_role_table(content)

    updated_roles: dict[str, dict[str, str]] = dict(current_roles)

    print("\n" + "-" * 65)
    print("⚙️  开始配置 5 大核心认知模态的承载方式:")

    for role_name, duty_desc in STANDARD_ROLES.items():
        curr = updated_roles.get(role_name, {})
        curr_cli = curr.get("cli", "subagent")
        curr_cmd = curr.get("cmd", "内置 Subagent 机制 (auto)")

        print(f"\n👉 角色: 【{role_name}】 ({duty_desc})")
        print(f"   当前配置: 承载 CLI = `{curr_cli}`, 启动命令 = `{curr_cmd}`")

        # 构造选项
        options: list[tuple[str, str, str]] = []  # (label, cli, cmd)
        options.append(("内置 Subagent (当前 Agent 自闭环默认)", "subagent", "内置 Subagent 机制 (auto)"))

        for agent in KNOWN_AGENTS:
            if agent.id == "subagent":
                continue
            if agent.id in installed_map:
                preset_cmd = agent.presets.get(role_name, f"{agent.cli} -p \"{{prompt}}\"")
                options.append((f"{agent.name} [推荐预设: `{preset_cmd}`]", agent.cli, preset_cmd))

        options.append(("自定义输入其他命令", "custom", ""))
        options.append(("保持当前配置不变", "keep", ""))

        print("   可选承载选项:")
        for idx, (label, _, _) in enumerate(options, 1):
            print(f"     [{idx}] {label}")

        try:
            choice_str = input(f"   请选择 [1-{len(options)}] (默认回车保持): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消配置。")
            return

        if not choice_str:
            continue

        try:
            choice_idx = int(choice_str) - 1
            if choice_idx < 0 or choice_idx >= len(options):
                print("   ⚠️ 无效选项，保持当前配置。")
                continue
        except ValueError:
            print("   ⚠️ 输入非数字，保持当前配置。")
            continue

        selected = options[choice_idx]
        if selected[1] == "keep":
            continue
        elif selected[1] == "custom":
            try:
                custom_cli = input("   请输入承载 CLI 名称 (如 omp / claude / my-agent): ").strip()
                custom_cmd = input(f"   请输入启动命令 (如 `{custom_cli} -p \"{{prompt}}\"`): ").strip()
                if custom_cli and custom_cmd:
                    updated_roles[role_name] = {
                        "duty": duty_desc,
                        "cli": custom_cli,
                        "cmd": custom_cmd,
                    }
                    print(f"   ✅ 已更新 {role_name} ➔ {custom_cli}")
            except (EOFError, KeyboardInterrupt):
                print("\n已跳过自定义输入。")
                continue
        else:
            updated_roles[role_name] = {
                "duty": duty_desc,
                "cli": selected[1],
                "cmd": selected[2],
            }
            print(f"   ✅ 已选择: {selected[0]}")

    ok = update_workspace_config(config_path, updated_roles)
    if ok:
        print("\n" + "=" * 65)
        print(f"🎉 角色配置已成功保存至: {config_path}")
        print("=" * 65)
        # 执行一次校验汇报
        verify_list = verify_roles(config_path)
        print("\n📋 最终生效状态:")
        for rep in verify_list:
            print(f"  • {rep['role']}: {rep['msg']}")
    else:
        print("\n❌ 保存配置文件失败。")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Entropaxis Agent 角色配置与探针工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--scan", "-s", action="store_true", help="扫描宿主机已安装的 Agent CLI")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出扫描或校验结果")
    parser.add_argument("--verify", "-v", action="store_true", help="校验当前 workspace-config.md 中的角色命令可用性")
    parser.add_argument("--apply-preset", metavar="AGENT", help="一键为所有角色应用指定 Agent 的预设 (如 omp/subagent/claude)")
    parser.add_argument("--set-role", nargs=3, metavar=("ROLE", "CLI", "CMD"), help="设定指定角色的承载 CLI 和启动命令")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="指定 workspace-config.md 路径")

    args = parser.parse_args()

    if args.scan:
        detected = detect_installed_agents()
        if args.json:
            print(json.dumps(detected, ensure_ascii=False, indent=2))
        else:
            print("🔍 宿主机 Agent CLI 检测报告:\n" + "=" * 50)
            for item in detected:
                status = "✅ 已安装" if item["installed"] else "❌ 未安装"
                print(f"{status} | {item['name']}")
                print(f"   CLI: `{item['cli']}`")
                if item["installed"]:
                    print(f"   路径: {item['path']}")
                    print(f"   版本: {item['version']}")
                print(f"   说明: {item['description']}")
                print()
        return 0

    if args.verify:
        reports = verify_roles(args.config)
        if args.json:
            print(json.dumps(reports, ensure_ascii=False, indent=2))
        else:
            print(f"🔍 正在核验角色配置: {args.config}\n" + "=" * 50)
            for r in reports:
                print(f"【{r.get('role')}】 (CLI: `{r.get('cli')}`)")
                print(f"  命令: {r.get('cmd')}")
                print(f"  状态: {r.get('msg')}")
                print()
        return 0

    if args.apply_preset:
        agent_id = args.apply_preset.lower().strip()
        matched = next((a for a in KNOWN_AGENTS if a.id == agent_id or a.cli == agent_id), None)
        if not matched:
            print(f"❌ 未知 Agent 预设: {args.apply_preset}", file=sys.stderr)
            print(f"👉 修复建议: 可选预设包括: {', '.join(a.id for a in KNOWN_AGENTS)}", file=sys.stderr)
            return 1

        new_roles: dict[str, dict[str, str]] = {}
        for role_name, duty in STANDARD_ROLES.items():
            preset_cmd = matched.presets.get(role_name, f"{matched.cli} -p \"{{prompt}}\"")
            new_roles[role_name] = {
                "duty": duty,
                "cli": matched.cli,
                "cmd": preset_cmd,
            }

        ok = update_workspace_config(args.config, new_roles)
        if ok:
            print(f"✅ 已成功将所有角色批量设置为 【{matched.name}】 推荐预设！")
            return 0
        return 1

    if args.set_role:
        role_name, cli_val, cmd_val = args.set_role
        content = args.config.read_text(encoding="utf-8") if args.config.exists() else ""
        roles = parse_role_table(content)
        duty = STANDARD_ROLES.get(role_name, roles.get(role_name, {}).get("duty", "业务协作"))
        roles[role_name] = {
            "duty": duty,
            "cli": cli_val,
            "cmd": cmd_val,
        }
        ok = update_workspace_config(args.config, roles)
        if ok:
            print(f"✅ 已成功更新角色 【{role_name}】 ➔ CLI: `{cli_val}`, 启动命令: `{cmd_val}`")
            return 0
        return 1

    # 如果没有传任何命令行参数，且处于终端 TTY 环境，进入交互向导
    if sys.stdin.isatty():
        interactive_wizard(args.config)
        return 0
    else:
        # 非交互环境默认执行 --verify 并输出
        reports = verify_roles(args.config)
        print(f"📋 当前角色配置生效状态 ({args.config}):\n")
        for r in reports:
            print(f"• {r.get('role')} ({r.get('cli')}): {r.get('msg')}")
        print("\n💡 提示: 在终端运行 `python3 .entropaxis/tools/setup_agents.py` 可进入交互式配置向导。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
