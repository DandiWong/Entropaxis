"""Ordered role preferences, invocation adapters and authorization-gated probes.

Configuration contains agent/model OR custom argv, never both. Local detection
never selects a profile or calls a provider. Live checks require a separate
explicit authorization event; an installed CLI or a catalog entry is not proof
of usable credentials/model access. No permission bypass flags are used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import tempfile
from typing import Any

AGENTS = ("claude", "codex", "pi", "omp", "agy", "opencode", "mimo")
HARD_ROLES = frozenset(("Reviewer", "Maintainer"))
PROMPT = "{PROMPT}"
UNSAFE = set(';&|`$><\n\r')

PROBE_SYSTEM_PROMPT = "You are an isolated model availability probe. Follow only the user message."
CONTEXT_UNISOLATED_PROBE_AGENTS = frozenset(("agy", "opencode", "mimo"))
PROCESS_REAP_TIMEOUT_S = 1
# 超时兜底链的最末一级：profile.timeout_s → roles.<Role>.timeout_s → default_timeout_s → 本值
FALLBACK_TIMEOUT_S = 900
# 硬门禁超时上限（级联雪崩与误阻断防护）；角色级与 profile 级覆盖同样受此约束
HARD_ROLE_MAX_TIMEOUT_S = 1800
MAX_TIMEOUT_S = 7200


def effective_timeout(data: dict[str, Any], profile: dict[str, Any], role: str | None = None) -> int:
    """解析一次调度实际使用的超时：profile > 角色 > 顶层 default > 900。

    同一 profile 可被多个角色共享，而超时按角色定（Builder 3600 vs Reviewer 1800 共用
    glm-5.3 就是实证）；因此取值必须带 role 解析，不能只读 profile 自身。缺 timeout_s
    不再让 subprocess 侧补默认——那会让超时事实分散在两处。
    """
    for value in (profile.get("timeout_s"), (data.get("roles", {}).get(role) or {}).get("timeout_s") if role else None,
                  data.get("default_timeout_s"), FALLBACK_TIMEOUT_S):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return FALLBACK_TIMEOUT_S


def profile_argv(profile: dict[str, Any]) -> list[str]:
    if "argv" in profile:
        if "agent" in profile or "model" in profile:
            raise ValueError("profile cannot contain both argv and agent/model")
        argv = profile["argv"]
        if not isinstance(argv, list) or not argv or any(not isinstance(t, str) or not t or set(t) & UNSAFE for t in argv):
            raise ValueError("invalid custom argv")
        return list(argv)
    agent, model = profile.get("agent"), profile.get("model")
    if agent not in AGENTS or not isinstance(model, str) or not model.strip() or model != model.strip() or set(model) & UNSAFE:
        raise ValueError("profile requires a supported agent and an exact nonempty model")
    if "fallback_profile" in profile:
        raise ValueError("agent/model profiles use roles.preferences, not fallback_profile")
    if agent == "codex":
        return [agent, "exec", "--model", model, PROMPT]
    if agent in ("opencode", "mimo"):
        return [agent, "run", "--model", model, PROMPT]
    return [agent, "--model", model, "-p", PROMPT]


def preference_profiles(data: dict[str, Any], role: str, selected_only: bool = True) -> list[tuple[str, dict[str, Any]]]:
    entry = data.get("roles", {}).get(role, {})
    selected = entry.get("profile")
    profiles = data.get("command_profiles", {})
    if "preferences" in entry:
        names = entry["preferences"]
        if selected_only:
            if selected is None:
                return []
            if selected not in names:
                raise ValueError(f"{role}: selected profile is not a preference")
            names = names[names.index(selected):]
        return [(name, profiles[name]) for name in names if name in profiles]
    names, seen = [], set()
    name = selected
    while name and name in profiles and name not in seen:
        seen.add(name)
        names.append(name)
        name = profiles[name].get("fallback_profile")
    return [(name, profiles[name]) for name in names]


def semantic_errors(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    profiles = data.get("command_profiles", {})
    for name, profile in profiles.items():
        try:
            profile_argv(profile)
        except (ValueError, TypeError) as exc:
            errors.append(f"{name}: {exc}")
        fallback = profile.get("fallback_profile")
        if fallback and fallback not in profiles:
            errors.append(f"{name}: unknown fallback_profile {fallback}")
    for role, entry in data.get("roles", {}).items():
        names = entry.get("preferences")
        selected = entry.get("profile")
        timeout = effective_timeout(data, {}, role)
        if not 1 <= timeout <= MAX_TIMEOUT_S:
            errors.append(f"{role}: 解析后的 timeout_s {timeout} 超出 1..{MAX_TIMEOUT_S}")
        elif role in HARD_ROLES and timeout > HARD_ROLE_MAX_TIMEOUT_S:
            errors.append(f"{role}: 超时 {timeout}s 超过硬门禁上限 {HARD_ROLE_MAX_TIMEOUT_S}s")
        if names is not None:
            if len(names) != len(set(names)):
                errors.append(f"{role}: duplicate preference reference")
            if selected is not None and selected not in names:
                errors.append(f"{role}: selected profile is not a preference")
            for name in names:
                if name not in profiles:
                    errors.append(f"{role}: unknown preference {name}")
                elif profiles[name].get("fallback_profile"):
                    errors.append(f"{role}: preference {name} also declares fallback_profile")
                elif PROMPT not in profile_argv_safe(profiles[name]):
                    errors.append(f"{role}: preference {name} has no task prompt")
        elif selected and selected not in profiles:
            errors.append(f"{role}: unknown selected profile {selected}")
        # 每个偏好单独解析（profile 覆盖可能高于角色级），硬门禁上限对三级取值一律生效
        for name, profile in preference_profiles(data, role, selected_only=False):
            resolved = effective_timeout(data, profile, role)
            if not 1 <= resolved <= MAX_TIMEOUT_S:
                errors.append(f"{role}/{name}: 解析后的 timeout_s {resolved} 超出 1..{MAX_TIMEOUT_S}")
            elif role in HARD_ROLES and resolved > HARD_ROLE_MAX_TIMEOUT_S:
                errors.append(f"{role}/{name}: 超时 {resolved}s 超过硬门禁上限 {HARD_ROLE_MAX_TIMEOUT_S}s")
    return errors


def profile_argv_safe(profile: dict[str, Any]) -> list[str]:
    try:
        return profile_argv(profile)
    except (ValueError, TypeError):
        return []


def migrate_config(data: dict[str, Any]) -> None:
    """Explicit migration, preserving profile IDs, selection and timeouts.

    `note` 已从契约删除（零读者，内容与配置值/规则正文重复会漂移）：迁移时一并剥掉，
    否则旧实例一跑写入命令就把已删字段带回来，schema 校验反被拖垮。
    """
    profiles = data.get("command_profiles", {})
    preference_names: set[str] = set()
    for role, entry in data.get("roles", {}).items():
        if "preferences" not in entry:
            entry["preferences"] = [name for name, _ in preference_profiles(data, role, selected_only=False)]
        preference_names.update(entry["preferences"])
    for name in preference_names:
        profile = profiles.get(name)
        if not profile:
            continue
        profile.pop("fallback_profile", None)
        profile.pop("note", None)
        argv = profile.get("argv", [])
        if not argv or argv[0] not in AGENTS:
            continue
        agent = argv[0]
        model = None
        for i, token in enumerate(argv[:-1]):
            if token in ("--model", "-m"):
                model = argv[i + 1]
                break
        if not model:
            continue
        candidate = {"agent": agent, "model": model}
        canonical = profile_argv(candidate)
        old_codex = [agent, "--model", model, "-p", PROMPT]
        if argv != canonical and not (agent == "codex" and argv == old_codex):
            continue  # Extra/custom arguments remain explicit argv, without guessing.
        profile.pop("argv")
        profile.update(candidate)


def _local_status(profile: dict[str, Any], cache: dict[tuple[str, ...], tuple[str, str]]) -> tuple[str, str]:
    try:
        argv = profile_argv(profile)
    except ValueError as exc:
        return "invalid", str(exc)
    executable = shutil.which(argv[0])
    if not executable:
        return "not_installed", "executable not found"
    if "agent" not in profile:
        return "unsupported", "custom argv requires manual verification"
    key = (executable,)
    if key not in cache:
        cache[key] = ("local_ready", "executable found; invocation and model access unverified")
    return cache[key]


def local_candidates(data: dict[str, Any], roles: list[str] | None = None) -> list[dict[str, Any]]:
    cache: dict[tuple[str, ...], tuple[str, str]] = {}
    reports = []
    for role in roles if roles is not None else data.get("roles", {}):
        entry = data.get("roles", {}).get(role)
        if entry is None:
            raise ValueError(f"unknown role {role}")
        preferences = []
        for number, (name, profile) in enumerate(preference_profiles(data, role, False), 1):
            status, reason = _local_status(profile, cache)
            preferences.append({"number": number, "profile": name, "agent": profile.get("agent"), "model": profile.get("model"), "status": status, "reason": reason})
        names = [item["profile"] for item in preferences]
        selected = entry.get("profile")
        reports.append({"role": role, "selected_profile": selected, "selected_preference": names.index(selected) + 1 if selected in names else None, "preferences": preferences})
    return reports


def _probe_argv(profile: dict[str, Any], prompt: str) -> list[str]:
    argv = [prompt if token == PROMPT else token for token in profile_argv(profile)]
    agent = profile["agent"]
    flags = {
        "claude": [
            "--output-format", "json",
            "--tools", "",
            "--bare",
            "--restricted",
            "--no-session-persistence",
            "--system-prompt", PROBE_SYSTEM_PROMPT,
        ],
        "codex": [
            "--json",
            "--sandbox", "read-only",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
        ],
        "pi": [
            "--mode", "json",
            "--no-session",
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--system-prompt", PROBE_SYSTEM_PROMPT,
        ],
        "omp": [
            "--mode", "json",
            "--no-session",
            "--no-tools",
            "--no-lsp",
            "--no-extensions",
            "--no-skills",
            "--no-rules",
            "--no-title",
            "--no-prewalk",
            "--system-prompt", PROBE_SYSTEM_PROMPT,
        ],
        "agy": ["--output-format", "json", "--mode", "plan", "--sandbox"],
        "opencode": ["--format", "json"],
        "mimo": ["--format", "json"],
    }
    return argv + flags[agent]


def _resolved_model(message: dict[str, Any]) -> str | None:
    provider = message.get("provider")
    model = message.get("responseModel") or message.get("model")
    if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
        return None
    return f"{provider}/{model}"


def _claude_resolved_model(event: dict[str, Any]) -> str | None:
    model_usage = event.get("modelUsage")
    if not isinstance(model_usage, dict):
        return None
    models = [model for model in model_usage if isinstance(model, str) and model]
    return models[0] if len(models) == 1 else None


def _assistant_response(stdout: str, agent: str) -> tuple[str | None, str | None]:
    """Extract a terminal assistant response and its protocol-reported identity."""
    texts: list[str] = []
    identities: set[str] = set()
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("is_error") or event.get("type") in ("error", "turn.failed"):
            return None, None
        if agent == "claude" and event.get("type") == "result" and isinstance(event.get("result"), str):
            texts.append(event["result"])
            identity = _claude_resolved_model(event)
            if identity is not None:
                identities.add(identity)
        elif agent == "codex" and event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                texts.append(item["text"])
        elif agent in ("pi", "omp") and event.get("type") == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant" and message.get("stopReason") not in ("error", "aborted"):
                text = "".join(
                    part["text"] for part in message.get("content", [])
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                )
                texts.append(text)
                identity = _resolved_model(message)
                if identity is not None:
                    identities.add(identity)
    response = "".join(texts)
    if not response:
        return None, None
    return response, next(iter(identities)) if len(identities) == 1 else None


def _terminate_probe_process(process: subprocess.Popen[str]) -> None:
    try:
        if os.name == "posix":
            # Pipe EOF does not prove descendants exited: kill the entire group.
            os.killpg(process.pid, signal.SIGKILL)
        else:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=PROCESS_REAP_TIMEOUT_S,
            )
    except (OSError, subprocess.TimeoutExpired):
        if process.poll() is None:
            process.kill()
    process.communicate()


def _probe_environment(agent: str, cwd: str, env: dict[str, str]) -> dict[str, str]:
    if agent != "codex":
        return env
    Path(cwd).chmod(0o700)
    codex_home = Path(cwd) / "codex-home"
    codex_home.mkdir(mode=0o700)
    source_home = Path(env.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    source_auth = source_home / "auth.json"
    if source_auth.is_file():
        probe_auth = codex_home / "auth.json"
        shutil.copyfile(source_auth, probe_auth)
        probe_auth.chmod(0o600)
    env["CODEX_HOME"] = str(codex_home)
    return env


def _probe_command(profile: dict[str, Any], prompt: str, cwd: str) -> list[str]:
    argv = _probe_argv(profile, prompt)
    if profile["agent"] == "codex":
        instructions = Path(cwd) / "codex-system-prompt.md"
        instructions.write_text(PROBE_SYSTEM_PROMPT, encoding="utf-8")
        argv.extend(("-c", f'experimental_instructions_file="{instructions}"'))
    if profile["agent"] == "omp":
        config = Path(cwd) / "probe-config.yml"
        config.write_text('codexResets:\n  autoRedeem: "no"\n', encoding="utf-8")
        argv.extend(("--config", str(config)))
    return argv


def _live_probe(profile: dict[str, Any], timeout: int) -> str:
    if profile["agent"] in CONTEXT_UNISOLATED_PROBE_AGENTS:
        return "context_unisolated"
    nonce = "ENTROPAXIS_" + secrets.token_hex(8)
    prompt = f"Reply with exactly {nonce}. Do not use tools, read files, or perform any other action."
    process: subprocess.Popen[str] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="entropaxis-role-live-") as cwd:
            env = _probe_environment(profile["agent"], cwd, os.environ.copy())
            process = subprocess.Popen(
                _probe_command(profile, prompt, cwd),
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=os.name == "posix",
            )
            try:
                stdout, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate_probe_process(process)
                return "timeout"
    except OSError:
        if process is not None:
            _terminate_probe_process(process)
        return "launch_failed"
    if process is None or process.returncode:
        return "call_failed"
    response, resolved_model = _assistant_response(stdout, profile["agent"])
    if response != nonce:
        return "invalid_response"
    if resolved_model is None:
        return "identity_missing"
    if resolved_model != profile["model"]:
        return "identity_mismatch"
    return "available"


def select_preferences(data: dict[str, Any], *, authorized: bool = False, authorization_event: str = "", roles: list[str] | None = None, timeout: int = 30) -> list[dict[str, Any]]:
    role_names = roles if roles is not None else list(data.get("roles", {}))
    if not authorized or not authorization_event.strip():
        return [{"role": role, "outcome": "authorization_required", "selected_profile": data.get("roles", {}).get(role, {}).get("profile")} for role in role_names]
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 120:
        raise ValueError("probe timeout must be 1..120 seconds")
    errors = semantic_errors(data)
    if errors:
        raise ValueError("; ".join(errors[:3]))
    reports = local_candidates(data, role_names)
    checked: dict[tuple[str, str], str] = {}
    for report in reports:
        role = report["role"]
        entry = data["roles"][role]
        report["authorization_event"] = authorization_event.strip()
        report["outcome"] = "no_available_preference"
        report["attempts"] = []
        for candidate in report["preferences"]:
            status = candidate["status"]
            profile = data["command_profiles"][candidate["profile"]]
            if status == "local_ready":
                key = (profile["agent"], profile["model"])
                if key not in checked:
                    checked[key] = _live_probe(profile, timeout)
                status = checked[key]
            report["attempts"].append({"number": candidate["number"], "profile": candidate["profile"], "status": status})
            if status == "available":
                entry["profile"] = candidate["profile"]
                report.update(outcome="selected", selected_profile=candidate["profile"], selected_preference=candidate["number"])
                break
    return reports
