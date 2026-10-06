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

  # 4. 一键为全部标准角色应用指定 Agent 的推荐预设
  python3 .entropaxis/tools/setup_agents.py --apply-preset omp
  python3 .entropaxis/tools/setup_agents.py --apply-preset subagent

  # 5. 精确设置指定角色的 CLI 和启动命令
  python3 .entropaxis/tools/setup_agents.py --set-role Reviewer omp "omp --model a/b || claude -p {PROMPT}"

  # 6. 角色清单：职责、门禁、承载链（CLI/模型/超时/存活态）
  python3 .entropaxis/tools/setup_agents.py --list [--json]

  # 7. 局部更新：只换主选模型 / 只调超时 / 只改职责，不动命令其余部分
  python3 .entropaxis/tools/setup_agents.py --set-model Reviewer openai-codex/gpt-6.1-sol
  python3 .entropaxis/tools/setup_agents.py --set-timeout Builder 3600
  python3 .entropaxis/tools/setup_agents.py --set-duty Researcher "前沿技术选型与实测查新"

  # 8. 删除自定义角色（标准 7 角色受保护；默认 dry-run，--yes 才落盘）
  python3 .entropaxis/tools/setup_agents.py --remove-role DataSteward
  python3 .entropaxis/tools/setup_agents.py --remove-role DataSteward --yes
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

# 硬门禁角色（与 dispatch_role.HARD_ROLES 同源，见 rules/角色协作.md「门禁划分」；
# 测试锚定两集合相等防漂移——setup_agents 不 import dispatch_role，避免其顶层 PyYAML 硬依赖）
HARD_GATED_ROLES = {"Reviewer", "Maintainer"}
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
    # 机器枚举可用模型（2026-10-07 调研）：agy/opencode/mimo 有列模型子命令；pi 需 pattern 搜索
    # 不自动枚举（claude/codex 无子命令，omp 未见）——预设表人工维护即"探测是候选不是默认"边界
    models_cmd: tuple[str, ...] = ()
    models_parse: str = ""  # "" 不枚举 | "tab" = 首列 model_id | "lines" = 每行 provider/model


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
            "Architecture": "omp --model openai-codex/gpt-6.1-sol",
            "Reviewer": "omp --model openai-codex/gpt-6.1-sol",
            "Researcher": "omp --model google-antigravity/gemini-3.8-flash || omp --model minimax-code-cn/MiniMax-M3",
            "Builder": "omp --model zhipu-coding-plan/glm-5.3 || claude --model sonnet-5",
            "Designer": "claude --model sonnet-5",
            "Maintainer": "claude --model opus-5",
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
    AgentInfo(
        id="pi",
        name="Pi coding agent (pi)",
        cli="pi",
        version_cmd=["pi", "--version"],
        description="多 provider 终端 coding agent（badlogic/pi-mono）；模型 pattern 支持 provider/id 与 :thinking 档位",
        # pi 无列模型子命令；/model 的 Ctrl+S 保存即默认模型，裸命令直接用该默认
        presets={role: "pi -p {prompt}" for role in STANDARD_ROLES},
    ),
    AgentInfo(
        id="agy",
        name="Google Antigravity CLI (agy)",
        cli="agy",
        version_cmd=["agy", "--version"],
        description="Google Antigravity 终端 agent（Gemini 系）；headless 出 JSON 信封（status/response/usage）",
        models_cmd=("agy", "models"),
        models_parse="tab",
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
        models_cmd=("opencode", "models"),
        models_parse="lines",
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
        models_cmd=("mimo", "models"),
        models_parse="lines",
        presets={  # headless 是 run <位置参数>，非 -p {PROMPT}
            "Builder": "mimo run --model xiaomi/mimo-v2.5-pro {prompt}",
        },
    ),
]


MODELS_LIMIT = 20  # 枚举仅作候选展示，截断防长输出（工具设计 Token 经济性）


def parse_model_lines(stdout: str, parse: str) -> list[str]:
    """列模型子命令 stdout → model id 列表。
    tab：agy 输出「model_id<Tab>显示名」，无 tab 的杂讯行（如 Fetching...）跳过；
    lines：opencode/mimo 输出「provider/model」每行一个，仅保留含 / 的行。"""
    ids: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        mid = line.split("\t")[0].strip() if parse == "tab" else line
        if parse == "tab" and "\t" not in line:
            continue
        if parse == "lines" and "/" not in mid:
            continue
        if mid and mid not in ids:
            ids.append(mid)
    return ids[:MODELS_LIMIT]


def probe_models(agent: AgentInfo) -> dict[str, Any]:
    """执行列模型子命令（软降级：失败/超时返回空列表并留痕，不阻断扫描）。"""
    if not agent.models_cmd or not agent.models_parse:
        return {}
    try:
        proc = subprocess.run(list(agent.models_cmd), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available_models": [], "models_error": f"{type(exc).__name__}"}
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return {"available_models": [], "models_error": (tail[0][:80] if tail else f"exit {proc.returncode}")}
    models = parse_model_lines(proc.stdout or "", agent.models_parse)
    out: dict[str, Any] = {"available_models": models}
    if len(models) == MODELS_LIMIT:
        out["models_truncated"] = True
    return out


def detect_installed_agents() -> list[dict[str, Any]]:
    """扫描系统环境检测已安装的 Agent CLI 及其版本信息（含可用模型枚举）。"""
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
        item |= probe_models(agent)  # 枚举仅对已装 CLI 生效；结果是候选，写入须用户显式选择
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

def _argv_model(argv: list[str]) -> str | None:
    """从结构化 argv 提取 --model/-m 的值；无该标志返回 None。"""
    for i, t in enumerate(argv):
        if t in MODEL_FLAGS and i + 1 < len(argv):
            return argv[i + 1]
    return None


def patch_model_in_argv(argv: list[str], new_model: str) -> list[str]:
    """就地置换主选模型：已有 --model/-m 换值；没有则插到首个 -p/{PROMPT} 之前
    （再没有则紧跟可执行名），其余 token 原样保留——避免整条命令重写丢占位符/备选链。"""
    tokens = list(argv)
    for i, t in enumerate(tokens):
        if t in MODEL_FLAGS and i + 1 < len(tokens):
            tokens[i + 1] = new_model
            return tokens
    insert_pair = ["--model", new_model]
    for i, t in enumerate(tokens):
        if t in ("-p", "--prompt") or t == PROMPT:
            return tokens[:i] + insert_pair + tokens[i:]
    return (tokens[:1] + insert_pair + tokens[1:]) if tokens else insert_pair


def _require_role(data: dict[str, Any], role: str) -> dict[str, Any]:
    entry = (data.get("roles") or {}).get(role)
    if entry is None:
        known = "、".join(sorted((data.get("roles") or {}))) or "（配置为空）"
        raise ConfigError(f"角色 {role} 不存在；现有: {known}\n👉 检查拼写，或先用 --set-role 新增")
    return entry


def _require_external_chain(data: dict[str, Any], role: str, action: str) -> list[tuple[str, dict[str, Any]]]:
    """局部更新只对外置承载有意义：内置 Subagent 无 argv 可改。"""
    entry = _require_role(data, role)
    chain = profile_chain(data, entry.get("profile"))
    if not chain:
        raise ConfigError(f"{role} 为内置 Subagent 承载（profile 为空），无命令可{action}\n"
                          f"👉 先 --set-role {role} <CLI> \"<命令>\" 配置外置承载")
    return chain


def set_model(data: dict[str, Any], role: str, model: str) -> None:
    """只换主选 profile 的模型，fallback 链不动（整链同换会让备选失去意义）。"""
    if not model.strip():
        raise ConfigError("模型名不能为空")
    chain = _require_external_chain(data, role, "置换模型")
    name, prof = chain[0]
    prof["argv"] = patch_model_in_argv(prof["argv"], model.strip())


def set_timeout(data: dict[str, Any], role: str, seconds: int) -> None:
    """角色超时 = 整条备选链统一调整（fallback 沿用旧超时会在主选放宽后率先 TIMEOUT）。
    硬门禁 ≤1800s（角色协作.md「超时与颗粒度约束」），全员 ≤7200s（command_profile.schema.json）。"""
    if role in HARD_GATED_ROLES and seconds > 1800:
        raise ConfigError(f"硬门禁角色 {role} 超时须 ≤1800s，实得 {seconds}s\n👉 拆小任务或换角色承载")
    if not 1 <= seconds <= 7200:
        raise ConfigError(f"timeout_s 须为 1~7200 的整数，实得 {seconds}")
    chain = _require_external_chain(data, role, "调整超时")
    for _, prof in chain:
        prof["timeout_s"] = seconds


def set_duty(data: dict[str, Any], role: str, duty: str) -> None:
    """定向更新角色职责（merge-only 下用户明确指令允许的字段级修改）。"""
    if not duty.strip():
        raise ConfigError("duty 不能为空")
    _require_role(data, role)["duty"] = duty.strip()


def remove_role(data: dict[str, Any], role: str) -> list[str]:
    """删除自定义角色并回收其独占 profile（被其他角色/备选链引用的保留）。
    返回被回收的 profile 名列表。标准 7 角色是认知模态体系基石，拒绝删除。"""
    if role in STANDARD_ROLES:
        raise ConfigError(f"标准角色 {role} 不可删除（认知模态体系，dispatch_role.py 依赖）\n"
                          f"👉 停用外置承载请改用 --set-role {role} subagent \"\"")
    entry = _require_role(data, role)
    old = [n for n, _ in profile_chain(data, entry.get("profile"))]
    data["roles"].pop(role)
    referenced = {r.get("profile") for r in data["roles"].values()} | {
        p.get("fallback_profile") for n, p in data["command_profiles"].items() if n not in old}
    removed: list[str] = []
    for name in old:
        if name not in referenced:
            data["command_profiles"].pop(name, None)
            removed.append(name)
    return removed


def list_roles(data: dict[str, Any]) -> list[dict[str, Any]]:
    """角色清单视图：标准角色在前（固定序），自定义角色字典序殿后。
    gate 取值 hard/soft/custom；custom 即 dispatch_role 不调度的非标准角色。"""
    roles = data.get("roles") or {}
    order = [r for r in STANDARD_ROLES if r in roles] + sorted(r for r in roles if r not in STANDARD_ROLES)
    items: list[dict[str, Any]] = []
    for role in order:
        entry = roles[role] or {}
        chain = profile_chain(data, entry.get("profile"))
        chain_view = [{"profile": n, "cli": Path(p["argv"][0]).name, "model": _argv_model(p["argv"]),
                       "timeout_s": p.get("timeout_s"), "executable": bool(shutil.which(p["argv"][0]))}
                      for n, p in chain]
        gate = "hard" if role in HARD_GATED_ROLES else ("soft" if role in STANDARD_ROLES else "custom")
        item: dict[str, Any] = {"role": role, "standard": role in STANDARD_ROLES, "gate": gate,
                                "duty": entry.get("duty", ""), "carrier": "external" if chain else "subagent",
                                "chain": chain_view}
        if chain:
            item |= {"profile": chain[0][0], "cli": chain_view[0]["cli"],
                     "model": chain_view[0]["model"], "timeout_s": chain_view[0]["timeout_s"]}
        items.append(item)
    return items


def _fmt_list_line(item: dict[str, Any]) -> str:
    tag = {"hard": "硬门禁", "soft": "软外置", "custom": "自定义·不被调度"}[item["gate"]]
    if item["carrier"] == "subagent":
        carrier = "内置 Subagent"
    else:
        segs = [f"{c['cli']}({c['model']})" if c["model"] else c["cli"] for c in item["chain"]]
        dead = [c["cli"] for c in item["chain"] if not c["executable"]]
        carrier = "外置 " + " → ".join(segs) + f" {item.get('timeout_s')}s" + (" ⚠️不在PATH:" + ",".join(dead) if dead else "")
    return f"{item['role']}: {tag} | {carrier} | duty={item['duty']}"


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
        if role not in STANDARD_ROLES:  # 配置层开放但调度层封闭（dispatch --role 限 7 角色），核验面必须可见
            base["standard"] = False
            reports.append({"role": role, "status": "custom_role_note",
                            "msg": f"⚠️ {role} 为非标准角色：dispatch_role.py --role 限 7 标准角色，此条目仅作承载声明。"})
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
    parser.add_argument("--list", action="store_true", help="角色清单：职责、门禁、承载链（CLI/模型/超时/PATH 存活态）")
    parser.add_argument("--set-model", nargs=2, metavar=("ROLE", "MODEL"), help="只换角色主选模型的 --model 值，命令其余部分与备选链不动")
    parser.add_argument("--set-timeout", nargs=2, metavar=("ROLE", "SEC"), help="调整角色整条承载链的 timeout_s（硬门禁角色 ≤1800）")
    parser.add_argument("--set-duty", nargs=2, metavar=("ROLE", "DUTY"), help="定向更新角色职责描述")
    parser.add_argument("--remove-role", metavar="ROLE", help="删除自定义角色并回收其独占 profile（标准 7 角色受保护）")
    parser.add_argument("--yes", action="store_true", help="配合 --remove-role 跳过 dry-run 预览直接执行")
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

    if args.list:
        items = list_roles(load_config(args.config))
        if args.json:
            print(json.dumps(items, ensure_ascii=False, separators=(",", ":")))
        else:
            for item in items:
                print(_fmt_list_line(item))
        return 0
    if args.verify:
        reports = verify_roles(args.config)
        if args.json:
            print(json.dumps(reports, ensure_ascii=False, separators=(",", ":")))
        else:
            for r in reports:
                print(f"{r.get('role')}: {r.get('msg')}")
        return 1 if any(r["status"] in ("schema_error", "profile_missing", "missing_config") for r in reports) else 0

    if args.remove_role:
        import copy
        preview = copy.deepcopy(load_config(args.config))
        try:
            removed = remove_role(preview, args.remove_role)
        except ConfigError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        if not args.yes:
            print(f"dry-run：将删除角色 {args.remove_role}"
                  + (f"，回收 profile: {', '.join(removed)}" if removed else "（无独占 profile 需回收）")
                  + "；未做任何改动，加 --yes 执行")
            return 0
        data = load_config(args.config)
        removed = remove_role(data, args.remove_role)
        try:
            save_config(args.config, data)
        except ConfigError as exc:
            print(f"❌ {exc}\n👉 未写入；修正后重试。", file=sys.stderr)
            return 1
        print(f"✅ 已删除角色 {args.remove_role}"
              + (f"，回收 profile: {', '.join(removed)}" if removed else ""))
        return 0
    if args.apply_preset or args.set_role or args.set_model or args.set_timeout or args.set_duty:
        data = load_config(args.config)
        try:
            if args.set_role:
                role_name, cli_val, cmd_val = args.set_role
                set_role(data, role_name, cli_val, cmd_val)
                changed = [role_name]
            elif args.set_model:
                role_name, model_val = args.set_model
                set_model(data, role_name, model_val)
                changed = [role_name]
            elif args.set_timeout:
                role_name, sec_val = args.set_timeout
                try:
                    sec = int(sec_val)
                except ValueError:
                    print(f"❌ 超时须为整数秒，实得 {sec_val!r}\n👉 例如 --set-timeout Builder 3600", file=sys.stderr)
                    return 1
                set_timeout(data, role_name, sec)
                changed = [role_name]
            elif args.set_duty:
                role_name, duty_val = args.set_duty
                set_duty(data, role_name, duty_val)
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
            if args.set_role and role_name not in STANDARD_ROLES:
                print(f"⚠️ {role_name} 非标准认知模态：dispatch_role.py --role 限 7 标准角色，此条目仅作承载声明；"
                      f"如需新认知模态走「系统演进」")
        return 0

    if sys.stdin.isatty():
        interactive_wizard(args.config)
        return 0
    for r in verify_roles(args.config):
        print(f"{r.get('role')}: {r.get('msg')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
