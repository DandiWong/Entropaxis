"""Ordered role preferences, invocation adapters and authorization-gated probes.

Configuration contains agent/model OR custom argv, never both. Local detection
never selects a profile or calls a provider. Live checks require a separate
explicit authorization event; an installed CLI or a catalog entry is not proof
of usable credentials/model access. No permission bypass flags are used.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
from typing import Any

AGENTS = ("claude", "codex", "pi", "omp", "agy", "opencode", "mimo")
HARD_ROLES = frozenset(("Reviewer", "Maintainer"))
PROMPT = "{PROMPT}"
UNSAFE = set(';&|`$><\n\r')


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
        for name, profile in preference_profiles(data, role, selected_only=False):
            timeout = profile.get("timeout_s")
            if isinstance(timeout, int) and role in HARD_ROLES and timeout > 1800:
                errors.append(f"{role}/{name}: hard-gate timeout exceeds 1800s")
    return errors


def profile_argv_safe(profile: dict[str, Any]) -> list[str]:
    try:
        return profile_argv(profile)
    except (ValueError, TypeError):
        return []


def migrate_config(data: dict[str, Any]) -> None:
    """Explicit migration, preserving profile IDs, selection, notes and timeouts."""
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
    agent = profile["agent"]
    help_argv = [executable, *(["exec"] if agent == "codex" else ["run"] if agent in ("opencode", "mimo") else []), "--help"]
    key = tuple(help_argv)
    if key not in cache:
        try:
            with tempfile.TemporaryDirectory(prefix="entropaxis-role-local-") as cwd:
                result = subprocess.run(help_argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
            help_text = result.stdout + result.stderr
            capable = result.returncode == 0 and "--model" in help_text
            if agent in ("claude", "pi", "omp", "agy"):
                capable = capable and ("-p" in help_text or "--print" in help_text)
            cache[key] = ("local_ready", "invocation supported; model access unverified") if capable else ("unsupported", "installed version lacks required invocation flags")
        except (OSError, subprocess.SubprocessError):
            cache[key] = ("unsupported", "local help probe failed")
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
        "claude": ["--output-format", "json", "--tools", "", "--no-session-persistence", "--setting-sources", ""],
        "codex": ["--json", "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check"],
        "pi": ["--mode", "json", "--no-session", "--no-tools", "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files"],
        "omp": ["--mode", "json", "--no-session", "--no-tools", "--no-extensions", "--no-skills", "--no-rules", "--no-title", "--no-prewalk"],
        "agy": ["--output-format", "json", "--mode", "plan", "--sandbox"],
        "opencode": ["--format", "json"],
        "mimo": ["--format", "json"],
    }
    return argv + flags[agent]


def _assistant_response(stdout: str, agent: str) -> str | None:
    """Extract only assistant responses, never echoed prompts or diagnostics."""
    texts: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("is_error") or event.get("type") in ("error", "turn.failed"):
            return None
        if agent == "agy" and event.get("status") == "SUCCESS" and isinstance(event.get("response"), str):
            texts.append(event["response"])
        elif agent == "claude" and event.get("type") == "result" and isinstance(event.get("result"), str):
            texts.append(event["result"])
        elif agent in ("opencode", "mimo") and event.get("type") == "text":
            text = event.get("part", {}).get("text")
            if isinstance(text, str):
                texts.append(text)
        elif agent == "codex" and event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                texts.append(item["text"])
        elif agent in ("pi", "omp") and event.get("type") == "message_end":
            message = event.get("message", {})
            if message.get("role") == "assistant" and message.get("stopReason") not in ("error", "aborted"):
                texts.extend(part["text"] for part in message.get("content", []) if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str))
    return "".join(texts).strip() if texts else None


def _live_probe(profile: dict[str, Any], timeout: int) -> str:
    nonce = "ENTROPAXIS_" + secrets.token_hex(8)
    prompt = f"Reply with exactly {nonce}. Do not use tools, read files, or perform any other action."
    env = os.environ.copy()
    # CLI-level denial adds protection to the prompt; no auto-approval or sandbox bypass.
    if profile["agent"] in ("opencode", "mimo"):
        key = "OPENCODE_CONFIG_CONTENT" if profile["agent"] == "opencode" else "MIMOCODE_CONFIG_CONTENT"
        env[key] = json.dumps({"permission": {"*": "deny"}})
    try:
        with tempfile.TemporaryDirectory(prefix="entropaxis-role-live-") as cwd:
            result = subprocess.run(_probe_argv(profile, prompt), cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout"
    except (OSError, subprocess.SubprocessError):
        return "launch_failed"
    if result.returncode:
        return "call_failed"
    return "available" if _assistant_response(result.stdout, profile["agent"]) == nonce else "invalid_response"


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
