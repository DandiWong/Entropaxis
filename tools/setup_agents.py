#!/usr/bin/env python3
"""角色模态与外部 Agent 交互配置工具 (Agent Role Setup & Discovery Tool).

用于检测宿主机环境中已安装的各类 Agent CLI（如 omp、claude、codex、gemini、cursor、aider 等），
提供 7 个标准角色的启动候选，并管理 roles.yaml 中的有序偏好。
agent/model 配对为调用真源；自定义命令及校验命令保留 argv。真实检测须另获用户授权。

用法:
  # 1. 扫描当前环境已安装的 Agent CLI
  python3 .entropaxis/tools/setup_agents.py --scan
  python3 .entropaxis/tools/setup_agents.py --scan --json

  # 2. 校验 roles.yaml 的 schema 与各角色承载链
  python3 .entropaxis/tools/setup_agents.py --verify

  # 3. 交互式向导配置各个角色的承载 CLI 与启动命令
  python3 .entropaxis/tools/setup_agents.py

  # 4. 一键为全部标准角色应用指定 Agent 的推荐预设
  python3 .entropaxis/tools/setup_agents.py --apply-preset omp
  python3 .entropaxis/tools/setup_agents.py --apply-preset subagent

  # 5. 精确设置指定角色的 CLI 和启动命令
  python3 .entropaxis/tools/setup_agents.py --set-role Reviewer omp "omp --model a/b || claude -p {PROMPT}"

  # 6. 角色清单：完整偏好顺序与当前选中序号
  python3 .entropaxis/tools/setup_agents.py --list [--json]

  # 7. 局部更新：选中偏好；可用 --preference NUMBER 指定其他位置
  python3 .entropaxis/tools/setup_agents.py --set-model Reviewer openai-codex/gpt-6.1-sol
  python3 .entropaxis/tools/setup_agents.py --set-timeout Builder 3600
  python3 .entropaxis/tools/setup_agents.py --set-duty Researcher "前沿技术选型与实测查新"

  # 8. 删除自定义角色（标准 7 角色受保护；默认 dry-run，--yes 才落盘）
  python3 .entropaxis/tools/setup_agents.py --remove-role DataSteward
  python3 .entropaxis/tools/setup_agents.py --remove-role DataSteward --yes

  # 9. 偏好编辑与两阶段检测（第二阶段必须先取得本次用户授权）
  python3 .entropaxis/tools/setup_agents.py --set-preference Researcher 2 pi google-antigravity/gemini-3.8-flash
  python3 .entropaxis/tools/setup_agents.py --move-preference Researcher 2 1
  python3 .entropaxis/tools/setup_agents.py --detect-preferences --json
  python3 .entropaxis/tools/setup_agents.py --check-preferences --authorize-model-check --authorization-event '<用户确认>' --json
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
    from . import paths, role_preferences
except ImportError:
    import paths
    import role_preferences

ROOT = paths.WORKSPACE_ROOT
DEFAULT_CONFIG_PATH = paths.DATA_DIR / "templates" / "roles.yaml"
TEMPLATE_PATH = paths.SYSTEM_DIR / "templates" / "instance" / "roles.template.yaml"
PROMPT = "{PROMPT}"
PROMPT_FLAG_FAMILIES = {"omp", "claude", "pi", "agy"}  # print-mode CLI 族
SHELL_METACHARS = set(';&|`$><\n')
ROLE_NAME_RE = re.compile(r"^[A-Z][A-Za-z]*$")

# 硬门禁角色（与 role_preferences.HARD_ROLES 及调度门禁一致）。
HARD_GATED_ROLES = role_preferences.HARD_ROLES
MODEL_FLAGS = ("--model", "-m")
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


# 启动命令候选；模型可用性必须通过单独授权的真实调用确认。
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
            "Architecture": "omp --model openai-codex/gpt-6.1-sol",
            "Reviewer": "omp --model openai-codex/gpt-6.1-sol",
            "Researcher": "omp --model google-antigravity/gemini-3.8-flash || omp --model minimax-code-cn/MiniMax-M3",
            "Builder": "omp --model zhipu-coding-plan/glm-5.3 || claude --model claude-sonnet-5.5",
            "Designer": "claude --model claude-sonnet-5.5",
            "Maintainer": "claude --model claude-opus-5.5",
            "Reporter": "omp --model google-antigravity/gemini-3.8-flash || omp --model minimax-code-cn/MiniMax-M3",
        },
    ),
    AgentInfo(
        id="claude",
        name="Claude Code (claude)",
        cli="claude",
        version_cmd=["claude", "--version"],
        description="Anthropic 官方终端 Agent 工具，擅长深度推理与架构设计",
        presets={
            "Architecture": "claude --model claude-opus-5.5",
            "Reviewer": "claude -p \"{prompt}\"",
            "Researcher": "claude -p \"{prompt}\"",
            "Builder": "claude",
            "Designer": "claude --model claude-sonnet-5.5",
            "Maintainer": "claude --model claude-opus-5.5",
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
    AgentInfo(
        id="pi",
        name="Pi coding agent (pi)",
        cli="pi",
        version_cmd=["pi", "--version"],
        description="多 provider 终端 coding agent（badlogic/pi-mono）；模型 pattern 支持 provider/id 与 :thinking 档位",
        presets={role: "pi -p {prompt}" for role in STANDARD_ROLES},
    ),
    AgentInfo(
        id="agy",
        name="Google Antigravity CLI (agy)",
        cli="agy",
        version_cmd=["agy", "--version"],
        description="Google Antigravity 终端 agent（Gemini 系）；headless 出 JSON 信封（status/response/usage）",
        presets={
            "Researcher": "agy --model gemini-3.8-flash-medium -p {prompt}",
            "Reporter": "agy --model gemini-3.8-flash-medium -p {prompt}",
            "Builder": "agy --model gemini-3.1-pro-high -p {prompt}",
        },
    ),
    AgentInfo(
        id="opencode",
        name="OpenCode (opencode)",
        cli="opencode",
        version_cmd=["opencode", "--version"],
        description="终端 coding agent（sst/opencode）；模型格式 provider/model，含大量 -free 档",
        presets={  # headless 是 run <位置参数>，非 -p {PROMPT}
            "Builder": "opencode run --model opencode/mimo-v2.6-flash-free {prompt}",
            "Researcher": "opencode run --model opencode/mimo-v2.6-flash-free {prompt}",
        },
    ),
    AgentInfo(
        id="mimo",
        name="Xiaomi MiMo Code (mimo)",
        cli="mimo",
        version_cmd=["mimo", "--version"],
        description="小米 MiMo Code 终端 coding assistant；模型格式 provider/model，配置文件 mimocode.json",
        presets={  # headless 是 run <位置参数>，非 -p {PROMPT}
            "Builder": "mimo run --model xiaomi/mimo-v2.5-pro {prompt}",
        },
    ),
]


def preset_command(agent: AgentInfo, role: str) -> str | None:
    command = agent.presets.get(role)
    if command is not None:
        return command
    if agent.id == "subagent":
        return ""
    if agent.cli in PROMPT_FLAG_FAMILIES:
        return f"{agent.cli} -p {{PROMPT}}"
    if agent.cli == "codex":
        return "codex exec {PROMPT}"
    if agent.cli in ("opencode", "mimo"):
        return f"{agent.cli} run {{PROMPT}}"
    return None


def detect_installed_agents() -> list[dict[str, Any]]:
    """扫描本地 CLI 与版本；模型目录查询会触发远程访问，绝不由扫描调用。"""
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

        item = {
            "id": agent.id,
            "name": agent.name,
            "cli": agent.cli,
            "installed": True,
            "version": version,
            "path": cli_path,
            "description": agent.description,
            "presets": agent.presets,
        }
        results.append(item)

    return results


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
    """只在结构与语义契约均成立时原子写入。"""
    errs = schema_errors(data)
    if not errs:
        errs = role_preferences.semantic_errors(data)
    if errs:
        raise ConfigError("roles.yaml 未通过校验：" + "；".join(errs[:3]))
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
    """沿旧 custom argv fallback_profile 展开，遇环即停。"""
    chain: list[tuple[str, dict[str, Any]]] = []
    profiles = data.get("command_profiles") or {}
    while name and name in profiles and all(existing != name for existing, _ in chain):
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
            cli = Path(tokens[0]).name
            if cli == "codex":
                if len(tokens) == 1 or tokens[1] != "exec":
                    tokens.insert(1, "exec")
                tokens.append(PROMPT)
            elif cli in ("opencode", "mimo") and len(tokens) > 1 and tokens[1] == "run":
                tokens.append(PROMPT)
            elif cli in PROMPT_FLAG_FAMILIES:
                tokens += ["-p", PROMPT]
            else:
                raise ConfigError(f"`{cand}` 缺 {{PROMPT}} 占位符，调度器无法注入任务文本；请在命令中写明位置")
        argvs.append(tokens)
    if not argvs:
        raise ConfigError("命令为空")
    return argvs


def _all_preferences(data: dict[str, Any], role: str) -> list[tuple[str, dict[str, Any]]]:
    try:
        return role_preferences.preference_profiles(data, role, selected_only=False)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc


def _profile_argv(profile: dict[str, Any]) -> list[str]:
    try:
        return role_preferences.profile_argv(profile)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc


def _profile_model(profile: dict[str, Any]) -> str | None:
    return profile.get("model") or _argv_model(profile.get("argv") or [])


def _profile_references(data: dict[str, Any], name: str) -> int:
    roles = data.get("roles") or {}
    profiles = data.get("command_profiles") or {}
    role_refs = sum(name == entry.get("profile") or name in (entry.get("preferences") or [])
                    for entry in roles.values())
    return role_refs + sum(1 for profile in profiles.values() if profile.get("fallback_profile") == name)


def _preference_name(data: dict[str, Any], role: str, number: int) -> str:
    profiles = data["command_profiles"]
    while f"{role.lower()}-{number}" in profiles:
        number += 1
    return f"{role.lower()}-{number}"


def _gc_profiles(data: dict[str, Any], candidates: list[str]) -> list[str]:
    """只回收本次脱离且未共享的 profile，绝不改写保留记录的备注或超时。"""
    profiles = data.get("command_profiles") or {}
    removed: list[str] = []
    for name in dict.fromkeys(candidates):
        if name in profiles and _profile_references(data, name) == 0:
            profiles.pop(name)
            removed.append(name)
    return removed


def _old_role_profiles(data: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    names = [*(entry.get("preferences") or [])]
    if entry.get("profile"):
        names.extend(name for name, _ in profile_chain(data, entry["profile"]))
    return list(dict.fromkeys(names))


def _raw_profile(old: dict[str, Any] | None, argv: list[str]) -> dict[str, Any]:
    profile: dict[str, Any] = {"argv": argv, "timeout_s": (old or {}).get("timeout_s", 900)}
    if (old or {}).get("note"):
        profile["note"] = old["note"]
    return profile


def _agent_profile(old: dict[str, Any] | None, agent: str, model: str) -> dict[str, Any]:
    profile: dict[str, Any] = {"agent": agent, "model": model, "timeout_s": (old or {}).get("timeout_s", 900)}
    if (old or {}).get("note"):
        profile["note"] = old["note"]
    return profile


def set_role(data: dict[str, Any], role: str, cli: str, cmd: str) -> None:
    """将命令候选写为有序偏好，不再创建 fallback_profile 链。"""
    role_preferences.migrate_config(data)
    if not ROLE_NAME_RE.match(role):
        raise ConfigError(f"角色名须为英文 PascalCase（如 Reviewer、DataSteward），实得 {role!r}")
    roles, profiles = data["roles"], data["command_profiles"]
    entry = roles.setdefault(role, {"duty": STANDARD_ROLES.get(role, "业务协作"), "preferences": [], "profile": None})
    entry.setdefault("duty", STANDARD_ROLES.get(role, "业务协作"))
    entry.setdefault("preferences", [])
    entry.setdefault("profile", None)
    old = _old_role_profiles(data, entry)
    if cli == "subagent" or "内置" in cmd or cmd.strip() in ("", "auto", "subagent"):
        entry["profile"] = None
        entry["preferences"] = []
        _gc_profiles(data, old)
        return

    argvs = command_to_argvs(cmd)
    existing = list(entry["preferences"])
    names: list[str] = []
    for number, argv in enumerate(argvs, 1):
        current = existing[number - 1] if number <= len(existing) else None
        name = current if current and _profile_references(data, current) == 1 else _preference_name(data, role, number)
        profiles[name] = _raw_profile(profiles.get(current) if current else None, argv)
        names.append(name)
    entry["preferences"] = names
    entry["profile"] = names[0]
    role_preferences.migrate_config(data)
    _gc_profiles(data, [name for name in old if name not in names])


def role_view(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """人读视图包含完整偏好序列；无选择时绝不把第一项伪装为选中项。"""
    view: dict[str, dict[str, Any]] = {}
    for role, entry in (data.get("roles") or {}).items():
        preferences = _all_preferences(data, role)
        selected = entry.get("profile")
        selected_number = next((i for i, (name, _) in enumerate(preferences, 1) if name == selected), None)
        commands = [" ".join(shlex.quote(token) for token in _profile_argv(profile)) for _, profile in preferences]
        selected_profile = (data.get("command_profiles") or {}).get(selected) if selected else None
        view[role] = {
            "duty": entry.get("duty", ""),
            "profile": selected or "",
            "selected_preference": selected_number,
            "cli": Path(_profile_argv(selected_profile)[0]).name if selected_profile else "subagent",
            "cmd": " || ".join(commands) if commands else "内置 Subagent 机制 (auto)",
            "preferences": [name for name, _ in preferences],
        }
    return view


def _argv_model(argv: list[str]) -> str | None:
    for i, token in enumerate(argv):
        if token in MODEL_FLAGS and i + 1 < len(argv):
            return argv[i + 1]
    return None


def patch_model_in_argv(argv: list[str], new_model: str) -> list[str]:
    tokens = list(argv)
    for i, token in enumerate(tokens):
        if token in MODEL_FLAGS and i + 1 < len(tokens):
            tokens[i + 1] = new_model
            return tokens
    insert_pair = ["--model", new_model]
    for i, token in enumerate(tokens):
        if token in ("-p", "--prompt") or token == PROMPT:
            return tokens[:i] + insert_pair + tokens[i:]
    return (tokens[:1] + insert_pair + tokens[1:]) if tokens else insert_pair


def _require_role(data: dict[str, Any], role: str) -> dict[str, Any]:
    entry = (data.get("roles") or {}).get(role)
    if entry is None:
        known = "、".join(sorted((data.get("roles") or {}))) or "（配置为空）"
        raise ConfigError(f"角色 {role} 不存在；现有: {known}\n👉 检查拼写，或先用 --set-role 新增")
    return entry


def _target_preference(data: dict[str, Any], role: str, number: int | None, action: str) -> tuple[str, dict[str, Any]]:
    entry = _require_role(data, role)
    preferences = _all_preferences(data, role)
    if not preferences:
        raise ConfigError(f"{role} 没有外置偏好，无法{action}\n👉 先 --set-preference 或 --set-role")
    if number is None:
        selected = entry.get("profile")
        found = next(((name, profile) for name, profile in preferences if name == selected), None)
        if found is None:
            raise ConfigError(f"{role} 当前未选中偏好；请用 --preference NUMBER 明确指定")
        return found
    if not 1 <= number <= len(preferences):
        raise ConfigError(f"{role} 偏好序号须为 1~{len(preferences)}，实得 {number}")
    return preferences[number - 1]

def _editable_preference(data: dict[str, Any], role: str, name: str, profile: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """复制共享 profile 后再编辑，避免一个角色的局部命令改写另一个角色。"""
    if _profile_references(data, name) == 1:
        return name, profile
    entry = _require_role(data, role)
    preferences = entry["preferences"]
    index = preferences.index(name)
    replacement = _preference_name(data, role, index + 1)
    copied = dict(profile)
    if "argv" in copied:
        copied["argv"] = list(copied["argv"])
    data["command_profiles"][replacement] = copied
    preferences[index] = replacement
    if entry.get("profile") == name:
        entry["profile"] = replacement
    return replacement, copied


def set_preference(data: dict[str, Any], role: str, number: int, agent: str, model: str) -> None:
    """替换既有序号或在末尾追加 agent/model 偏好；共享 profile 一律复制后替换。"""
    role_preferences.migrate_config(data)
    entry = _require_role(data, role)
    agent, model = agent.strip(), model.strip()
    if not agent or not model:
        raise ConfigError("Agent 与模型名不能为空")
    try:
        role_preferences.profile_argv({"agent": agent, "model": model})
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    preferences = entry.setdefault("preferences", [])
    if not 1 <= number <= len(preferences) + 1:
        raise ConfigError(f"{role} 偏好序号只能替换 1~{len(preferences)} 或追加 {len(preferences) + 1}，实得 {number}")
    current = preferences[number - 1] if number <= len(preferences) else None
    name = current if current and _profile_references(data, current) == 1 else _preference_name(data, role, number)
    data["command_profiles"][name] = _agent_profile(data["command_profiles"].get(current) if current else None, agent, model)
    if current:
        preferences[number - 1] = name
        if entry.get("profile") == current:
            entry["profile"] = name
        _gc_profiles(data, [current] if current != name else [])
    else:
        preferences.append(name)


def set_model(data: dict[str, Any], role: str, model: str, preference: int | None = None) -> None:
    role_preferences.migrate_config(data)
    if not model.strip():
        raise ConfigError("模型名不能为空")
    name, profile = _target_preference(data, role, preference, "置换模型")
    _, profile = _editable_preference(data, role, name, profile)
    if profile.get("agent"):
        try:
            role_preferences.profile_argv({"agent": profile["agent"], "model": model.strip()})
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        profile["model"] = model.strip()
    else:
        profile["argv"] = patch_model_in_argv(profile["argv"], model.strip())


def set_timeout(data: dict[str, Any], role: str, seconds: int, preference: int | None = None) -> None:
    role_preferences.migrate_config(data)
    if role in HARD_GATED_ROLES and seconds > 1800:
        raise ConfigError(f"硬门禁角色 {role} 超时须 ≤1800s，实得 {seconds}s\n👉 拆小任务或换角色承载")
    if not 1 <= seconds <= 7200:
        raise ConfigError(f"timeout_s 须为 1~7200 的整数，实得 {seconds}")
    name, profile = _target_preference(data, role, preference, "调整超时")
    _, profile = _editable_preference(data, role, name, profile)
    profile["timeout_s"] = seconds


def set_duty(data: dict[str, Any], role: str, duty: str) -> None:
    role_preferences.migrate_config(data)
    if not duty.strip():
        raise ConfigError("duty 不能为空")
    _require_role(data, role)["duty"] = duty.strip()


def remove_preference(data: dict[str, Any], role: str, number: int) -> str:
    role_preferences.migrate_config(data)
    entry = _require_role(data, role)
    preferences = entry.get("preferences") or []
    if not 1 <= number <= len(preferences):
        raise ConfigError(f"{role} 偏好序号须为 1~{len(preferences)}，实得 {number}")
    removed = preferences.pop(number - 1)
    if entry.get("profile") == removed:
        entry["profile"] = None
    _gc_profiles(data, [removed])
    return removed


def move_preference(data: dict[str, Any], role: str, source: int, target: int) -> None:
    role_preferences.migrate_config(data)
    preferences = _require_role(data, role).get("preferences") or []
    if not 1 <= source <= len(preferences) or not 1 <= target <= len(preferences):
        raise ConfigError(f"{role} 偏好序号须为 1~{len(preferences)}，实得 {source}→{target}")
    profile = preferences.pop(source - 1)
    preferences.insert(target - 1, profile)


def remove_role(data: dict[str, Any], role: str) -> list[str]:
    role_preferences.migrate_config(data)
    if role in STANDARD_ROLES:
        raise ConfigError(f"标准角色 {role} 不可删除（认知模态体系，dispatch_role.py 依赖）")
    old = _old_role_profiles(data, _require_role(data, role))
    data["roles"].pop(role)
    return _gc_profiles(data, old)


def _preference_item(number: int, name: str, profile: dict[str, Any]) -> dict[str, Any]:
    argv = _profile_argv(profile)
    return {
        "number": number, "profile": name, "agent": profile.get("agent") or Path(argv[0]).name,
        "model": _profile_model(profile), "timeout_s": profile.get("timeout_s"),
        "executable": bool(shutil.which(argv[0])),
    }


def list_roles(data: dict[str, Any]) -> list[dict[str, Any]]:
    roles = data.get("roles") or {}
    order = [role for role in STANDARD_ROLES if role in roles] + sorted(role for role in roles if role not in STANDARD_ROLES)
    items: list[dict[str, Any]] = []
    for role in order:
        entry = roles[role] or {}
        preferences = [_preference_item(number, name, profile) for number, (name, profile) in enumerate(_all_preferences(data, role), 1)]
        selected = entry.get("profile")
        selected_number = next((item["number"] for item in preferences if item["profile"] == selected), None)
        gate = "hard" if role in HARD_GATED_ROLES else ("soft" if role in STANDARD_ROLES else "custom")
        item: dict[str, Any] = {
            "role": role, "standard": role in STANDARD_ROLES, "gate": gate, "duty": entry.get("duty", ""),
            "carrier": "external" if preferences else "subagent", "preferences": preferences,
            "selected_preference": selected_number,
        }
        if selected_number:
            selected_item = preferences[selected_number - 1]
            item |= {key: selected_item[key] for key in ("profile", "agent", "model", "timeout_s", "executable")}
            item["cli"] = selected_item["agent"]
        items.append(item)
    return items


def _fmt_list_line(item: dict[str, Any]) -> str:
    tag = {"hard": "硬门禁", "soft": "软外置", "custom": "自定义·不被调度"}[item["gate"]]
    if item["carrier"] == "subagent":
        carrier = "内置 Subagent"
    else:
        sequence = " → ".join(f"{p['number']}.{p['agent']}({p['model']})" for p in item["preferences"])
        carrier = f"外置 [{sequence}]；选中={item['selected_preference'] or '未选择'}"
    return f"{item['role']}: {tag} | {carrier} | duty={item['duty']}"


def verify_roles(config_path: Path) -> list[dict[str, Any]]:
    """仅报告本地 CLI 可执行性；真实模型可用性仅由授权检查确认。"""
    if not config_path.exists():
        return [{"role": "ALL", "status": "missing_config", "msg": f"配置文件不存在: {config_path}（运行 bootstrap.py 初始化）"}]
    data = load_config(config_path)
    errors = schema_errors(data)
    if not errors:
        errors = role_preferences.semantic_errors(data)
    if errors:
        return [{"role": "ALL", "status": "schema_error", "msg": "；".join(errors)}]
    reports: list[dict[str, Any]] = []
    for item in list_roles(data):
        base = {"role": item["role"], "selected_preference": item["selected_preference"]}
        if not item["standard"]:
            reports.append({"role": item["role"], "status": "custom_role_note",
                            "msg": f"⚠️ {item['role']} 为非标准角色：dispatch_role.py --role 限标准角色，此条目仅作承载声明。"})
        if not item["preferences"]:
            reports.append({**base, "status": "subagent_default", "msg": "✅ 内置 Subagent 承载（默认）。"})
        elif not item["selected_preference"]:
            reports.append({**base, "status": "preference_unselected", "msg": "⚠️ 有外置偏好但尚未选中 profile。"})
        elif item["executable"]:
            reports.append({**base, "status": "local_executable",
                            "msg": "✅ 选中偏好 CLI 可本地启动；此检查不验证模型权限。"})
        else:
            reports.append({**base, "status": "external_missing", "msg": "⚠️ 选中偏好 CLI 不在 PATH。"})
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
                preset = preset_command(agent, role_name)
                if preset is not None:
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
    try:
        role_preferences.migrate_config(data)
        save_config(config_path, data)
    except ConfigError as exc:
        print(f"\n⚠️ {exc}；未写入。")
        return
    print(f"\n✅ 已写入 {config_path}")
    for rep in verify_roles(config_path):
        print(f"  • {rep['role']}: {rep['msg']}")


def _emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    elif isinstance(value, list):
        for item in value:
            print(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
    else:
        print(value)


def _validation_errors(data: dict[str, Any]) -> list[str]:
    errors = schema_errors(data)
    return errors if errors else role_preferences.semantic_errors(data)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Entropaxis 角色承载配置工具（data/templates/roles.yaml）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--scan", "-s", action="store_true", help="扫描宿主机已安装的 Agent CLI（不查询模型目录）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--verify", "-v", action="store_true", help="校验配置与选中偏好的本地 CLI 状态")
    parser.add_argument("--list", action="store_true", help="角色清单及完整有序偏好")
    parser.add_argument("--detect-preferences", action="store_true", help="仅本地检查每项偏好的启动能力，不调用模型")
    parser.add_argument("--check-preferences", action="store_true", help="按偏好顺序检查真实模型响应（须单独授权）")
    parser.add_argument("--authorize-model-check", action="store_true", help="确认本次允许真实模型检查")
    parser.add_argument("--authorization-event", default="", help="本次模型检查的用户授权事件标识")
    parser.add_argument("--dry-run", action="store_true", help="只预览配置改动；模型检查时只做本地检测")
    parser.add_argument("--migrate-preferences", action="store_true", help="显式迁移旧 fallback_profile 链为有序偏好")
    parser.add_argument("--set-preference", nargs=4, metavar=("ROLE", "NUMBER", "AGENT", "MODEL"),
                        help="替换偏好位置或在末尾追加 agent/model 偏好")
    parser.add_argument("--remove-preference", nargs=2, metavar=("ROLE", "NUMBER"),
                        help="删除偏好；默认 dry-run，加 --yes 执行")
    parser.add_argument("--move-preference", nargs=3, metavar=("ROLE", "FROM", "TO"),
                        help="移动一个偏好到新序号，不改写 profile")
    parser.add_argument("--preference", type=int, metavar="NUMBER", help="--set-model/--set-timeout 的目标偏好序号")
    parser.add_argument("--set-model", nargs=2, metavar=("ROLE", "MODEL"), help="更新一个偏好的模型")
    parser.add_argument("--set-timeout", nargs=2, metavar=("ROLE", "SEC"), help="更新一个偏好的 timeout_s")
    parser.add_argument("--set-duty", nargs=2, metavar=("ROLE", "DUTY"), help="定向更新角色职责描述")
    parser.add_argument("--remove-role", metavar="ROLE", help="删除自定义角色并回收其独占 profile")
    parser.add_argument("--yes", action="store_true", help="配合删除命令跳过 dry-run 预览直接执行")
    parser.add_argument("--apply-preset", metavar="AGENT", help="一键为全部标准角色应用 Agent 预设")
    parser.add_argument("--set-role", nargs=3, metavar=("ROLE", "CLI", "CMD"),
                        help="设定角色承载：CMD 可用 || 串有序偏好；CLI 为 subagent 时改回内置承载")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="roles.yaml 路径")
    args = parser.parse_args()

    if args.scan:
        detected = detect_installed_agents()
        if args.json:
            _emit(detected, True)
        else:
            for item in detected:
                print(f"{'✅' if item['installed'] else '—'} {item['id']}: {item['path'] or '未安装'}")
        return 0

    if args.list:
        try:
            items = list_roles(load_config(args.config))
        except ConfigError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        if args.json:
            _emit(items, True)
        else:
            for item in items:
                print(_fmt_list_line(item))
        return 0

    if args.verify:
        reports = verify_roles(args.config)
        if args.json:
            _emit(reports, True)
        else:
            for report in reports:
                print(f"{report.get('role')}: {report.get('msg')}")
        return 1 if any(report["status"] in ("schema_error", "missing_config") for report in reports) else 0

    if args.detect_preferences or args.check_preferences:
        data = load_config(args.config)
        errors = _validation_errors(data)
        if errors:
            print(f"❌ {'；'.join(errors[:3])}", file=sys.stderr)
            return 1
        try:
            if args.detect_preferences or args.dry_run:
                reports = role_preferences.local_candidates(data)
            elif not args.authorize_model_check or not args.authorization_event.strip():
                reports = role_preferences.select_preferences(data)
            else:
                reports = role_preferences.select_preferences(
                    data, authorized=True, authorization_event=args.authorization_event
                )
                if any(report.get("outcome") == "selected" for report in reports):
                    save_config(args.config, data)
        except (ConfigError, ValueError) as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        _emit(reports, args.json)
        return 0 if args.detect_preferences or args.dry_run or all(
            report.get("outcome") != "authorization_required" for report in reports
        ) else 1

    if args.migrate_preferences:
        data = load_config(args.config)
        try:
            role_preferences.migrate_config(data)
            errors = _validation_errors(data)
            if errors:
                raise ConfigError("；".join(errors[:3]))
            if args.dry_run:
                _emit({"dry_run": True, "action": "migrate_preferences", "roles": list_roles(data)}, args.json)
                return 0
            save_config(args.config, data)
        except (ConfigError, ValueError, TypeError, KeyError) as exc:
            print(f"❌ {exc}\n👉 未写入；修正命令后重试。", file=sys.stderr)
            return 1
        _emit({"migrated": str(args.config)} if args.json else f"✅ 已迁移偏好: {args.config}", args.json)
        return 0

    if args.remove_role or args.remove_preference:
        import copy
        preview = copy.deepcopy(load_config(args.config))
        try:
            if args.remove_role:
                removed = remove_role(preview, args.remove_role)
                description = f"将删除角色 {args.remove_role}" + (f"，回收 profile: {', '.join(removed)}" if removed else "")
            else:
                role, number_text = args.remove_preference
                removed_name = remove_preference(preview, role, int(number_text))
                description = f"将删除 {role} 的偏好 {number_text}（{removed_name}）"
        except (ConfigError, ValueError) as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        if not args.yes or args.dry_run:
            _emit({"dry_run": True, "action": description} if args.json else f"dry-run：{description}；未做任何改动，加 --yes 执行", args.json)
            return 0
        data = load_config(args.config)
        try:
            if args.remove_role:
                remove_role(data, args.remove_role)
            else:
                role, number_text = args.remove_preference
                remove_preference(data, role, int(number_text))
            save_config(args.config, data)
        except (ConfigError, ValueError) as exc:
            print(f"❌ {exc}\n👉 未写入；修正命令后重试。", file=sys.stderr)
            return 1
        _emit({"removed": description} if args.json else f"✅ 已执行：{description}", args.json)
        return 0

    if args.apply_preset or args.set_role or args.set_preference or args.move_preference or args.set_model or args.set_timeout or args.set_duty:
        data = load_config(args.config)
        try:
            if args.set_role:
                role_name, cli_val, cmd_val = args.set_role
                set_role(data, role_name, cli_val, cmd_val)
                changed = [role_name]
            elif args.set_preference:
                role_name, number_text, agent, model = args.set_preference
                set_preference(data, role_name, int(number_text), agent, model)
                changed = [role_name]
            elif args.move_preference:
                role_name, source_text, target_text = args.move_preference
                move_preference(data, role_name, int(source_text), int(target_text))
                changed = [role_name]
            elif args.set_model:
                role_name, model_val = args.set_model
                set_model(data, role_name, model_val, args.preference)
                changed = [role_name]
            elif args.set_timeout:
                role_name, sec_val = args.set_timeout
                set_timeout(data, role_name, int(sec_val), args.preference)
                changed = [role_name]
            elif args.set_duty:
                role_name, duty_val = args.set_duty
                set_duty(data, role_name, duty_val)
                changed = [role_name]
            else:
                agent_id = args.apply_preset.lower().strip()
                matched = next((agent for agent in KNOWN_AGENTS if agent_id in (agent.id, agent.cli)), None)
                if not matched:
                    raise ConfigError(f"未知 Agent 预设: {args.apply_preset}；可选: {', '.join(agent.id for agent in KNOWN_AGENTS)}")
                for role_name in STANDARD_ROLES:
                    command = preset_command(matched, role_name)
                    if command is None:
                        raise ConfigError(f"{matched.id} 没有 {role_name} 的启动候选；请用 --set-role 明确配置")
                    set_role(data, role_name, matched.cli, command)
                changed = list(STANDARD_ROLES)
            errors = _validation_errors(data)
            if errors:
                raise ConfigError("；".join(errors[:3]))
            if args.dry_run:
                _emit({"dry_run": True, "changed": changed, "roles": [role_view(data)[role] for role in changed]}, args.json)
                return 0
            save_config(args.config, data)
        except (ConfigError, ValueError) as exc:
            print(f"❌ {exc}\n👉 未写入；修正命令后重试。", file=sys.stderr)
            return 1
        if args.json:
            _emit({"changed": changed, "roles": [role_view(data)[role] for role in changed]}, True)
        else:
            view = role_view(data)
            for role_name in changed:
                print(f"✅ {role_name} → {view[role_name]['cmd']}")
        return 0

    if sys.stdin.isatty():
        interactive_wizard(args.config)
        return 0
    for report in verify_roles(args.config):
        print(f"{report.get('role')}: {report.get('msg')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
