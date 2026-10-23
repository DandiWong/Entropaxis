"""Ordered role preferences, invocation adapters and authorization-gated probes.

Configuration contains agent/model OR custom argv, never both. Detection and
dispatch only read the configuration, never write it; roles.<role>.preferences
is the sole knob (dispatch always starts at the 1st). Local detection never
calls a provider. Live checks require a separate
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

USAGE_FLAGS: dict[str, list[str]] = {
    "claude": ["--output-format", "json"],
    "codex": ["--json"],
    "omp": ["--mode", "json"],
    "pi": ["--mode", "json"],
}

PROBE_SYSTEM_PROMPT = "You are an isolated model availability probe. Follow only the user message."
CONTEXT_UNISOLATED_PROBE_AGENTS = frozenset(("agy", "opencode", "mimo"))
PROCESS_REAP_TIMEOUT_S = 1
# 超时兜底链的最末一级：profile.timeout_s → roles.<Role>.timeout_s → default_timeout_s → 本值
FALLBACK_TIMEOUT_S = 900
# 硬门禁超时上限（级联雪崩与误阻断防护）；角色级与 profile 级覆盖同样受此约束。
# 上限按角色定，不取两者的共同最小值：Reviewer 要审大批量产物需长时窗口，
# Maintainer 只跑快速集成验收，长挂只会拖住交付，所以维持 1800s 快速失败。
HARD_ROLE_TIMEOUT_CAP_S = {"Reviewer": 3600, "Maintainer": 1800}
MAX_TIMEOUT_S = 7200


def hard_role_max_timeout(role: str | None) -> int:
    """硬门禁角色的超时上限；非硬门禁角色返回 schema 上限。

    真源唯一：校验器、CLI 写入与调度执行三层都取本函数，避免各写一份字面量后漂移。
    """
    return HARD_ROLE_TIMEOUT_CAP_S.get(role or "", MAX_TIMEOUT_S)


def effective_timeout(data: dict[str, Any], profile: dict[str, Any], role: str | None = None) -> int:
    """解析一次调度实际使用的超时：profile > 角色 > 顶层 default > 900。

    同一 profile 可被多个角色共享，而超时按角色定（同一 glm-5.3 配对上，Builder 与
    Reviewer 各取各的角色级超时）；因此取值必须带 role 解析，不能只读 profile 自身。
    缺 timeout_s 不再让 subprocess 侧补默认——那会让超时事实分散在两处。
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


def preference_profiles(data: dict[str, Any], role: str) -> list[tuple[str, dict[str, Any]]]:
    entry = data.get("roles", {}).get(role, {})
    profiles = data.get("command_profiles", {})
    if "preferences" in entry:
        return [(name, profiles[name]) for name in entry["preferences"] if name in profiles]
    # 旧实例（无 preferences）：沿旧 profile 的 fallback_profile 链展开，仅供迁移使用
    names, seen = [], set()
    name = entry.get("profile")
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
        timeout = effective_timeout(data, {}, role)
        if not 1 <= timeout <= MAX_TIMEOUT_S:
            errors.append(f"{role}: 解析后的 timeout_s {timeout} 超出 1..{MAX_TIMEOUT_S}")
        elif role in HARD_ROLES and timeout > hard_role_max_timeout(role):
            errors.append(f"{role}: 超时 {timeout}s 超过硬门禁上限 {hard_role_max_timeout(role)}s")
        if names is not None:
            if len(names) != len(set(names)):
                errors.append(f"{role}: duplicate preference reference")
            for name in names:
                if name not in profiles:
                    errors.append(f"{role}: unknown preference {name}")
                elif profiles[name].get("fallback_profile"):
                    errors.append(f"{role}: preference {name} also declares fallback_profile")
                elif PROMPT not in profile_argv_safe(profiles[name]):
                    errors.append(f"{role}: preference {name} has no task prompt")
        # 每个偏好单独解析（profile 覆盖可能高于角色级），硬门禁上限对三级取值一律生效
        for name, profile in preference_profiles(data, role):
            resolved = effective_timeout(data, profile, role)
            if not 1 <= resolved <= MAX_TIMEOUT_S:
                errors.append(f"{role}/{name}: 解析后的 timeout_s {resolved} 超出 1..{MAX_TIMEOUT_S}")
            elif role in HARD_ROLES and resolved > hard_role_max_timeout(role):
                errors.append(f"{role}/{name}: 超时 {resolved}s 超过硬门禁上限 {hard_role_max_timeout(role)}s")
    return errors


def profile_argv_safe(profile: dict[str, Any]) -> list[str]:
    try:
        return profile_argv(profile)
    except (ValueError, TypeError):
        return []


def migrate_config(data: dict[str, Any]) -> list[str]:
    """Explicit migration, preserving profile IDs and timeouts; returns human-readable notes.

    旧 `roles.<角色>.profile`（当前选中偏好）字段在此删除：若它不是 preferences[0]，
    挪到首位以保持原有默认承载不变；为 null 的角色迁移后改按第 1 偏好调度，须在说明里点明。

    `note` 已从契约删除（零读者，内容与配置值/规则正文重复会漂移）：迁移时一并剥掉，
    否则旧实例一跑写入命令就把已删字段带回来，schema 校验反被拖垮。
    """
    profiles = data.get("command_profiles", {})
    preference_names: set[str] = set()
    notes: list[str] = []
    for role, entry in data.get("roles", {}).items():
        if "preferences" not in entry:
            entry["preferences"] = [name for name, _ in preference_profiles(data, role)]
        if "profile" in entry:
            selected = entry.pop("profile")
            names = entry["preferences"]
            if selected in names and names.index(selected) != 0:
                names.remove(selected)
                names.insert(0, selected)
                notes.append(f"{role}: 旧 profile {selected} 不是第 1 偏好，已挪到首位以保持原有默认承载不变")
            elif selected is None and names:
                notes.append(f"{role}: 旧 profile 为 null（原未选中，会话内承载）；迁移后按第 1 偏好 {names[0]} 调度，要保持会话内承载请改 preferences 为空")
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
    return notes


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
        for number, (name, profile) in enumerate(preference_profiles(data, role), 1):
            status, reason = _local_status(profile, cache)
            preferences.append({"number": number, "profile": name, "agent": profile.get("agent"), "model": profile.get("model"), "status": status, "reason": reason})
        reports.append({"role": role, "preferences": preferences})
    return reports


def _probe_argv(profile: dict[str, Any], prompt: str) -> list[str]:
    argv = [prompt if token == PROMPT else token for token in profile_argv(profile)]
    agent = profile["agent"]
    flags = {
        "claude": [
            *USAGE_FLAGS["claude"],
            "--tools", "",
            "--bare",
            "--restricted",
            "--no-session-persistence",
            "--system-prompt", PROBE_SYSTEM_PROMPT,
        ],
        "codex": [
            *USAGE_FLAGS["codex"],
            "--sandbox", "read-only",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
        ],
        "pi": [
            *USAGE_FLAGS["pi"],
            "--no-session",
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--system-prompt", PROBE_SYSTEM_PROMPT,
        ],
        "omp": [
            *USAGE_FLAGS["omp"],
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


def parse_usage(stdout: str, agent: str) -> dict[str, Any] | None:
    """Parse agent process structured JSON/JSONL output into unified usage dictionary.

    Returns:
        {"session_id": str, "models": {model_name: {"in": int, "out": int, "cache_read": int, "cache_write": int}}, "cost_usd": float | None}
        or None on error / unsupported agent / empty / garbage.
    """
    if not isinstance(stdout, str) or not stdout.strip():
        return None
    if agent not in ("claude", "codex", "omp", "pi"):
        return None

    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(event, dict):
            return None
        if event.get("is_error") or event.get("type") in ("error", "turn.failed"):
            return None
        events.append(event)

    if not events:
        return None

    if agent == "claude":
        result_event = None
        for ev in reversed(events):
            if ev.get("type") == "result" or "modelUsage" in ev:
                result_event = ev
                break
        if result_event is None and len(events) == 1:
            result_event = events[0]
        if not isinstance(result_event, dict):
            return None

        session_id = result_event.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return None

        model_usage = result_event.get("modelUsage")
        if not isinstance(model_usage, dict) or not model_usage:
            return None

        models: dict[str, dict[str, int]] = {}
        for m_name, u in model_usage.items():
            if not isinstance(u, dict):
                continue
            models[str(m_name)] = {
                "in": int(u.get("inputTokens") or 0),
                "out": int(u.get("outputTokens") or 0),
                "cache_read": int(u.get("cacheReadInputTokens") or 0),
                "cache_write": int(u.get("cacheCreationInputTokens") or 0),
            }
        if not models:
            return None

        cost_usd_raw = result_event.get("total_cost_usd")
        if cost_usd_raw is None:
            cost_usd_raw = result_event.get("cost_usd")
        cost_usd = float(cost_usd_raw) if isinstance(cost_usd_raw, (int, float)) else None

        return {
            "session_id": session_id,
            "models": models,
            "cost_usd": cost_usd,
        }

    elif agent == "codex":
        session_id = None
        turns: list[dict[str, Any]] = []
        for ev in events:
            if ev.get("type") == "thread.started":
                session_id = ev.get("thread_id")
            elif ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
                turns.append(ev["usage"])

        if not session_id or not turns:
            return None

        total_in = 0
        total_out = 0
        total_cache_read = 0
        total_cache_write = 0

        for u in turns:
            inp = int(u.get("input_tokens") or 0)
            cached = int(u.get("cached_input_tokens") or 0)
            out = int(u.get("output_tokens") or 0)
            total_in += max(0, inp - cached)
            total_cache_read += cached
            total_out += out

        return {
            "session_id": session_id,
            "models": {
                "": {
                    "in": total_in,
                    "out": total_out,
                    "cache_read": total_cache_read,
                    "cache_write": total_cache_write,
                }
            },
            "cost_usd": None,
        }

    elif agent in ("omp", "pi"):
        session_id = None
        for ev in events:
            if ev.get("type") == "session" and "id" in ev:
                session_id = ev.get("id")
                break
        if not session_id:
            return None

        models: dict[str, dict[str, int]] = {}
        total_cost = 0.0
        has_cost = False

        for ev in events:
            if ev.get("type") != "message_end":
                continue
            msg = ev.get("message")
            if not isinstance(msg, dict):
                continue
            m_name = msg.get("model") or ""
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            if m_name not in models:
                models[m_name] = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
            models[m_name]["in"] += int(usage.get("input") or 0)
            models[m_name]["out"] += int(usage.get("output") or 0)
            models[m_name]["cache_read"] += int(usage.get("cacheRead") or 0)
            models[m_name]["cache_write"] += int(usage.get("cacheWrite") or 0)

            cost_info = usage.get("cost")
            if isinstance(cost_info, dict) and "total" in cost_info and isinstance(cost_info["total"], (int, float)):
                total_cost += float(cost_info["total"])
                has_cost = True
            elif isinstance(cost_info, (int, float)):
                total_cost += float(cost_info)
                has_cost = True

        if not models:
            return None

        return {
            "session_id": session_id,
            "models": models,
            "cost_usd": round(total_cost, 6) if has_cost else None,
        }

    return None

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
    """只读：逐个实测每个偏好并报告，`first_available_preference` 为首个可用序号（无则 None）。"""
    role_names = roles if roles is not None else list(data.get("roles", {}))
    if not authorized or not authorization_event.strip():
        return [{"role": role, "outcome": "authorization_required"} for role in role_names]
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 120:
        raise ValueError("probe timeout must be 1..120 seconds")
    errors = semantic_errors(data)
    if errors:
        raise ValueError("; ".join(errors[:3]))
    reports = local_candidates(data, role_names)
    checked: dict[tuple[str, str], str] = {}
    for report in reports:
        report["authorization_event"] = authorization_event.strip()
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
        first = next((a["number"] for a in report["attempts"] if a["status"] == "available"), None)
        report.update(outcome="reported", first_available_preference=first)
    return reports
