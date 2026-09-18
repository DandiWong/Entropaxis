#!/usr/bin/env python3
"""角色指派三级解析与强约束调度 · 唯一执行入口。

真源定义: rules/角色协作.md「角色指派三级解析与调度门禁」「调度优先序与降级契约」
机器契约: schemas/roles_manifest.schema.json + command_profile.schema.json
实施方案: data/docs/20260918_角色指派三级解析与强约束调度方案/01_方案.md (approved, revision 4)

设计不变量（违反任何一条即 fail-closed）:
  1. mode 单向收紧——档案级只允许 strict；graceful 仅第 3 级有效授权，越权/过期/含硬门禁角色一律按 strict;
  2. 档案零命令——命令本体只存第 3 级 command_profiles，档案只存 profile 标识；
  3. 调度五态与审计终态分离——无 waived，豁免仅问题级（经 check_audit_gate.py 校验）;
  4. 并发安全——目录锁 + revision CAS + os.replace 原子写；journal 仅追加审计轨迹，不作恢复载体
     （恢复源是档案自身，见方案 §2.5 人工仲裁 B 收缩）。

退出码: 0=成功 / 2=BLOCKED(硬门禁穷尽，须问题级豁免) / 3=LOCAL_CARRY(软门禁本地承载) / 4=用法或校验错误
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - 环境缺 PyYAML 时给出可行动错误
    print("❌ dispatch_role.py 需要 PyYAML 解析 YAML 档案: pip install pyyaml", file=sys.stderr)
    raise

try:
    from . import paths
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import paths  # type: ignore

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
WS_CONFIG = SYSTEM_ROOT / "data" / "templates" / "workspace-config.md"
ACTIVE_CONFIG: Path | None = None  # 测试注入点；生产环境留空用 WS_CONFIG
SCHEMA_DIR = SYSTEM_ROOT / "schemas"

HARD_ROLES = {"Reviewer", "Maintainer"}
ALL_ROLES = ("Architecture", "Researcher", "Designer", "Builder", "Reviewer", "Maintainer", "Reporter")
SCHED_STATES = ("pending", "running", "succeeded", "failed", "blocked")
FAILURE_CODES = ("UNCONFIGURED", "SUBAGENT_AUTO", "NOT_EXECUTABLE", "TIMEOUT", "NO_VALID_OUTPUT", "CHAIN_EXHAUSTED")
# 聚合失败码优先级：最能说明「为什么没跑成」的排前
_FAILURE_PRIORITY = {"UNCONFIGURED": 0, "SUBAGENT_AUTO": 1, "NO_VALID_OUTPUT": 2, "TIMEOUT": 3, "NOT_EXECUTABLE": 4, "CHAIN_EXHAUSTED": 5}
SHELL_METACHARS = set(';&|`$><\n')
PROMPT_PLACEHOLDER = "{PROMPT}"
LEASE_SECONDS = 600
KNOWN_EXEC_BASENAMES = {"omp", "claude", "codex"}  # 迁移器可安全补 -p {PROMPT} 的可执行族

EXIT_OK, EXIT_BLOCKED, EXIT_LOCAL, EXIT_USAGE = 0, 2, 3, 4


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 第 3 级配置

_DISPATCH_BLOCK_RE = re.compile(r"```yaml\n(.*?command_profiles:.*?)```", re.DOTALL)


def load_dispatch_config(config_path: Path | None = None) -> dict[str, Any]:
    """从 workspace-config.md 提取调度参数 yaml 块（default_dispatch_mode/command_profiles/dispatch_authorizations）。"""
    config_path = config_path or ACTIVE_CONFIG or WS_CONFIG
    if not config_path.exists():
        return {"default_dispatch_mode": None, "command_profiles": {}, "dispatch_authorizations": [], "path": str(config_path), "present": False}
    m = _DISPATCH_BLOCK_RE.search(config_path.read_text(encoding="utf-8"))
    if not m:
        return {"default_dispatch_mode": None, "command_profiles": {}, "dispatch_authorizations": [], "path": str(config_path), "present": False}
    data = yaml.safe_load(m.group(1)) or {}
    return {
        "default_dispatch_mode": data.get("default_dispatch_mode"),
        "command_profiles": data.get("command_profiles") or {},
        "dispatch_authorizations": data.get("dispatch_authorizations") or [],
        "path": str(config_path),
        "present": True,
    }


def graceful_grant_valid(config: dict[str, Any], role: str, now: datetime | None = None) -> tuple[bool, str]:
    """校验 graceful 授权记录（§2.8）：缺确认事件/过期/越范围/含硬门禁角色 → 整条无效。"""
    now = now or datetime.now(timezone.utc)
    for grant in config.get("dispatch_authorizations") or []:
        gid = grant.get("id", "?")
        scope = grant.get("scope") or {}
        roles = scope.get("roles") or []
        if role in HARD_ROLES or any(r in HARD_ROLES for r in roles):
            return False, f"授权 {gid} 范围含硬门禁角色，整条无效"
        if role not in roles:
            continue
        if not grant.get("granted_by") or not grant.get("confirm_event"):
            return False, f"授权 {gid} 缺 granted_by/confirm_event，无效"
        until = grant.get("scope", {}).get("until")
        if until:
            try:
                exp = datetime.fromisoformat(until)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if now > exp:
                    return False, f"授权 {gid} 已于 {until} 过期"
            except ValueError:
                return False, f"授权 {gid} 的 until 非法: {until!r}"
        return True, f"授权 {gid} 有效"
    return False, "无覆盖该角色的授权记录"


# ---------------------------------------------------------------- 档案定位与校验

def find_manifest(cwd: Path) -> tuple[int, Path | None, dict[str, Any] | None]:
    """三级就近定位：1 capsule.yaml::roles_manifest（向上遍历）→ 2 workers.yaml（向上遍历）→ 3 None。"""
    for d in [cwd, *cwd.resolve().parents]:
        cap = d / "capsule.yaml"
        if cap.exists():
            data = yaml.safe_load(cap.read_text(encoding="utf-8")) or {}
            man = data.get("roles_manifest")
            if isinstance(man, dict) and man.get("assignments"):
                return 1, cap, man
    for d in [cwd, *cwd.resolve().parents]:
        w = d / "workers.yaml"
        if w.exists():
            data = yaml.safe_load(w.read_text(encoding="utf-8")) or {}
            man = data.get("roles_manifest")
            if isinstance(man, dict) and man.get("assignments"):
                return 2, w, man
    return 3, None, None


def validate_manifest(man: dict[str, Any]) -> list[str]:
    """结构校验（与 schemas/roles_manifest.schema.json 同构；一致性由 tests 样本集证明）。"""
    errs: list[str] = []
    if not isinstance(man, dict):
        return ["manifest 非对象"]
    allowed = {"mode", "assigned_at", "assigned_by", "revision", "assignments"}
    for k in man:
        if k not in allowed:
            errs.append(f"未知字段: {k}")
    if man.get("mode") not in (None, "strict"):
        errs.append(f"mode 只允许 strict 或省略（单向收紧）: {man.get('mode')!r}")
    for req in ("assigned_at", "assigned_by", "revision", "assignments"):
        if req not in man:
            errs.append(f"缺必填字段: {req}")
    if not isinstance(man.get("revision"), int) or isinstance(man.get("revision"), bool) or man.get("revision", -1) < 0:
        errs.append("revision 须为非负整数")
    if not isinstance(man.get("assignments"), list):
        errs.append("assignments 须为列表")
        return errs
    ids: set[str] = set()
    for i, a in enumerate(man.get("assignments") or []):
        where = f"assignments[{i}]"
        if not isinstance(a, dict):
            errs.append(f"{where} 非对象")
            continue
        a_allowed = {"assignment_id", "role", "command_profile", "target_path", "target_sha256", "deliverable", "status", "attempts", "deliverable_sha256"}
        for k in a:
            if k not in a_allowed:
                errs.append(f"{where} 未知字段: {k}")
        for req in ("assignment_id", "role", "command_profile", "target_path", "target_sha256", "deliverable", "status"):
            if req not in a:
                errs.append(f"{where} 缺必填字段: {req}")
        if a.get("role") not in ALL_ROLES:
            errs.append(f"{where} role 非法: {a.get('role')!r}")
        if a.get("status") not in SCHED_STATES:
            errs.append(f"{where} status 非法（无 waived，豁免仅问题级）: {a.get('status')!r}")
        aid = a.get("assignment_id")
        if aid in ids:
            errs.append(f"{where} assignment_id 重复: {aid!r}")
        ids.add(aid)
        if a.get("target_sha256") and not re.fullmatch(r"[0-9a-f]{64}", str(a["target_sha256"])):
            errs.append(f"{where} target_sha256 非 64 位十六进制")
        if a.get("status") == "succeeded" and not a.get("deliverable_sha256"):
            errs.append(f"{where} succeeded 必须携带 deliverable_sha256")
        for j, att in enumerate(a.get("attempts") or []):
            for req in ("profile", "argv_sha256", "started_at", "ended_at"):
                if req not in att:
                    errs.append(f"{where}.attempts[{j}] 缺必填字段: {req}")
            if att.get("failure_code") and att["failure_code"] not in FAILURE_CODES:
                errs.append(f"{where}.attempts[{j}] failure_code 非法: {att['failure_code']!r}")
    return errs


# ---------------------------------------------------------------- CAS 档案存储

class ManifestStore:
    """目录锁 + revision CAS + os.replace 原子写。journal 仅追加审计轨迹，不作恢复载体。"""

    def __init__(self, archive: Path):
        self.archive = archive
        self.lock_dir = archive.parent / f".{archive.name}.lock"
        self.journal = archive.parent / f".{archive.name}.journal.jsonl"

    def _read_raw(self) -> tuple[dict[str, Any], dict[str, Any]]:
        data = yaml.safe_load(self.archive.read_text(encoding="utf-8")) or {}
        return data, data.get("roles_manifest") or {}

    def acquire(self, owner: str, lease_s: int = LEASE_SECONDS) -> bool:
        try:
            self.lock_dir.mkdir()
        except FileExistsError:
            try:
                meta = json.loads((self.lock_dir / "owner.json").read_text(encoding="utf-8"))
                lease = datetime.fromisoformat(meta["lease_until"])
                if lease.tzinfo is None:
                    lease = lease.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) <= lease:
                    return False
            except (OSError, ValueError, KeyError):
                pass
            # 过期锁恢复：归档锁目录（永不静默删除），档案自身即最后一致态
            stale = self.archive.parent / f".{self.archive.name}.lock.stale-{int(time.time())}"
            os.rename(self.lock_dir, stale)
            self.lock_dir.mkdir()
        (self.lock_dir / "owner.json").write_text(
            json.dumps({"owner": owner, "lease_until": (datetime.now(timezone.utc) + timedelta(seconds=lease_s)).isoformat()}, ensure_ascii=False),
            encoding="utf-8",
        )
        return True

    def release(self) -> None:
        shutil.rmtree(self.lock_dir, ignore_errors=True)

    def update(self, mutator, owner: str) -> dict[str, Any]:
        """持锁 + revision 匹配的原子更新。mutator(manifest) 就地修改；返回更新后 manifest。"""
        data, man = self._read_raw()
        rev = man.get("revision")
        mutator(man)
        if man.get("revision") != rev + 1:
            raise RuntimeError(f"CAS 失败: revision 未按 +1 递增（读 {rev}，写 {man.get('revision')}）——请重读合并重试")
        new_block = yaml.safe_dump({"roles_manifest": man}, allow_unicode=True, sort_keys=False, default_flow_style=False).strip()
        text = self.archive.read_text(encoding="utf-8")
        if re.search(r"^roles_manifest:", text, re.MULTILINE):
            text = re.sub(r"^roles_manifest:.*?(?=^\S|\Z)", lambda _: new_block + "\n\n", text, flags=re.DOTALL | re.MULTILINE)
        else:
            text = text.rstrip() + "\n\n" + new_block + "\n"
        fd, tmp = tempfile.mkstemp(dir=self.archive.parent, prefix=f".{self.archive.name}.tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self.archive)  # 原子替换
        with open(self.journal, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _now_iso(), "owner": owner, "revision": man["revision"],
                                "assignments": [{"id": a.get("assignment_id"), "status": a.get("status")} for a in man.get("assignments", [])]},
                               ensure_ascii=False) + "\n")
        return man

    def stale_lock_report(self) -> list[Path]:
        return sorted(self.archive.parent.glob(f".{self.archive.name}.lock.stale-*"))


# ---------------------------------------------------------------- profile 解析与执行

def _argv_ok(argv: list[str]) -> bool:
    return bool(argv) and all(isinstance(t, str) and t and not (set(t) & SHELL_METACHARS) for t in argv)


def resolve_profile_chain(profile_name: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    """按 fallback_profile 单跳展开备选链（链深 ≤3、带环检测）。"""
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    name = profile_name
    profiles = config.get("command_profiles") or {}
    while name and name not in seen and len(chain) < 3:
        if name in seen:
            break
        seen.add(name)
        prof = profiles.get(name)
        if not isinstance(prof, dict):
            break
        chain.append({"name": name, **prof})
        name = prof.get("fallback_profile") or ""
    return chain


def _tier3_default_chain(role: str, config: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """第 3 级默认解析：约定命名 profile 优先，否则读角色表（setup_agents.parse_role_table）。"""
    profiles = config.get("command_profiles") or {}
    canon = f"{role.lower()}-primary"
    if canon in profiles:
        return canon, resolve_profile_chain(canon, config)
    try:
        sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
        from setup_agents import parse_role_table  # noqa: PLC0415
        table = parse_role_table(Path(config["path"]).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 表缺失按未配置处理
        return "UNCONFIGURED", []
    cmd = (table.get(role) or {}).get("cmd", "")
    if any(k in cmd for k in ("subagent", "auto", "内置")):
        return "SUBAGENT_AUTO", []
    tokens = cmd.split()
    if not tokens:
        return "UNCONFIGURED", []
    prof = {"name": f"{role.lower()}-table", "argv": tokens, "timeout_s": 900}
    return "TABLE", [prof]


def build_argv(profile: dict[str, Any], prompt: str) -> list[str] | None:
    argv = list(profile.get("argv") or [])
    if PROMPT_PLACEHOLDER not in argv:
        return None  # 无占位符 → 无法注入任务文本，按不可用处理
    out = [prompt if t == PROMPT_PLACEHOLDER else t for t in argv]
    return out if _argv_ok(out) else None


def _executable(argv0: str) -> str | None:
    if os.sep in argv0 or (os.altsep and os.altsep in argv0):
        p = Path(argv0)
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    return shutil.which(argv0)


def deliverable_valid(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    if path.suffix == ".md":
        return any(re.match(r"^#{1,6} ", ln) for ln in path.read_text(encoding="utf-8", errors="replace").splitlines())
    return True


def execute_chain(chain: list[dict[str, Any]], prompt: str, cwd: Path, deliverable: Path) -> tuple[str, list[dict[str, Any]], bool]:
    """执行备选链，返回 (聚合失败码, attempts, succeeded)。"""
    attempts: list[dict[str, Any]] = []
    codes: set[str] = set()
    for prof in chain:
        started = _now_iso()
        argv = build_argv(prof, prompt)
        if argv is None:
            attempts.append({"profile": prof["name"], "argv_sha256": _sha256_str(json.dumps(prof.get("argv"))), "started_at": started,
                             "ended_at": _now_iso(), "failure_code": "NOT_EXECUTABLE", "detail": "argv 缺 {PROMPT} 占位符或含 shell 元字符"})
            codes.add("NOT_EXECUTABLE")
            continue
        exe = _executable(argv[0])
        if exe is None:
            attempts.append({"profile": prof["name"], "argv_sha256": _sha256_str(json.dumps(argv)), "started_at": started,
                             "ended_at": _now_iso(), "failure_code": "NOT_EXECUTABLE", "detail": f"{argv[0]!r} 不可执行"})
            codes.add("NOT_EXECUTABLE")
            continue
        try:
            proc = subprocess.run([exe, *argv[1:]], cwd=str(cwd), capture_output=True, text=True,
                                  timeout=int(prof.get("timeout_s", 900)))
            ended = _now_iso()
            if proc.returncode == 0 and deliverable_valid(deliverable):
                attempts.append({"profile": prof["name"], "argv_sha256": _sha256_str(json.dumps(argv)), "started_at": started,
                                 "ended_at": ended, "exit_code": 0, "report_sha256": _sha256_file(deliverable)})
                return "", attempts, True
            code = "NO_VALID_OUTPUT" if proc.returncode == 0 else "CHAIN_EXHAUSTED"
            attempts.append({"profile": prof["name"], "argv_sha256": _sha256_str(json.dumps(argv)), "started_at": started,
                             "ended_at": ended, "exit_code": proc.returncode, "failure_code": code,
                             "detail": (proc.stderr or "")[-400:]})
            codes.add(code)
        except subprocess.TimeoutExpired:
            attempts.append({"profile": prof["name"], "argv_sha256": _sha256_str(json.dumps(argv)), "started_at": started,
                             "ended_at": _now_iso(), "failure_code": "TIMEOUT", "timeout": True})
            codes.add("TIMEOUT")
    if not codes:
        codes.add("CHAIN_EXHAUSTED")
    aggregate = min(codes, key=lambda c: _FAILURE_PRIORITY[c])
    return aggregate, attempts, False


# ---------------------------------------------------------------- 矩阵裁决

def adjudicate(mode: str, gate: str, failure_code: str) -> dict[str, Any]:
    """调度决策矩阵唯一实现（角色协作.md「角色指派三级解析与调度门禁」）。"""
    if gate == "hard":
        return {"outcome": "blocked", "exit": EXIT_BLOCKED,
                "reason": f"{mode} × 硬门禁 × {failure_code} → blocked；仅问题级 waived_by_user（经 check_audit_gate.py 校验）可解锁会话内承载"}
    if mode == "strict" and failure_code in ("UNCONFIGURED", "SUBAGENT_AUTO"):
        return {"outcome": "local_carry", "exit": EXIT_LOCAL, "needs_external_review": False,
                "reason": f"strict × 软门禁 × {failure_code} → 本地承载 + attempts 记 unconfigured→local"}
    if mode == "strict":
        return {"outcome": "local_carry", "exit": EXIT_LOCAL, "needs_external_review": True,
                "reason": f"strict × 软门禁 × {failure_code} → 本地承载 + 留痕 + 待外置复核（计入 Maintainer 终审必查）"}
    return {"outcome": "local_carry", "exit": EXIT_LOCAL, "needs_external_review": False,
            "reason": f"graceful × 软门禁 × {failure_code} → 本地承载 + 留痕"}


def resolve_mode(man: dict[str, Any] | None, config: dict[str, Any], role: str) -> tuple[str, str]:
    if man and man.get("mode") == "strict":
        return "strict", "档案级显式 strict"
    dflt = config.get("default_dispatch_mode")
    if dflt == "graceful":
        ok, why = graceful_grant_valid(config, role)
        return ("graceful", why) if ok else ("strict", f"第 3 级声明 graceful 但{why} → 收紧为 strict")
    if dflt not in ("strict", None):
        return "strict", f"default_dispatch_mode 非法值 {dflt!r} → 硬默认 strict"
    return "strict", "第 3 级缺省/strict → 硬默认 strict"


# ---------------------------------------------------------------- 主流程

def run_assignment(cwd: Path, assignment_id: str, prompt: str, ack: str | None, owner: str) -> int:
    cwd = cwd.resolve()
    tier, archive, man = find_manifest(cwd)
    if archive is None or man is None:
        print(json.dumps({"error": "无第 1/2 级指派档案", "hint": "三级解析落到第 3 级默认，run 需要显式指派（方案 §2.2-4 建档条件）"}, ensure_ascii=False))
        return EXIT_USAGE
    config = load_dispatch_config()
    target = [a for a in man.get("assignments", []) if a.get("assignment_id") == assignment_id]
    if not target:
        print(json.dumps({"error": f"assignment_id 不存在: {assignment_id}", "known": [a.get("assignment_id") for a in man.get("assignments", [])]}, ensure_ascii=False))
        return EXIT_USAGE
    a = target[0]
    errs = validate_manifest(man)
    if errs:
        print(json.dumps({"error": "manifest 校验失败", "issues": errs}, ensure_ascii=False))
        return EXIT_USAGE
    role = a["role"]
    mode, mode_why = resolve_mode(man, config, role)
    gate = "hard" if role in HARD_ROLES else "soft"
    target_file = (archive.parent / a["target_path"]).resolve()
    if not target_file.exists():
        print(json.dumps({"error": f"target_path 不存在: {target_file}"}, ensure_ascii=False))
        return EXIT_USAGE
    actual = _sha256_file(target_file)
    if actual != a["target_sha256"]:
        print(json.dumps({"error": "target_sha256 与当前对象不符", "declared": a["target_sha256"], "actual": actual,
                          "rule": "succeeded 失效回 pending（方案 §2.5）；请更新指纹后重跑"}, ensure_ascii=False))
        return EXIT_USAGE
    deliverable = (archive.parent / a["deliverable"]).resolve()

    chain = resolve_profile_chain(a["command_profile"], config)
    source = "profile"
    if not chain:
        source, chain = _tier3_default_chain(role, config)
        if source in ("UNCONFIGURED", "SUBAGENT_AUTO"):
            failure, attempts = source, []
            succeeded = False
        else:
            failure, attempts, succeeded = execute_chain(chain, prompt, cwd, deliverable)
    else:
        failure, attempts, succeeded = execute_chain(chain, prompt, cwd, deliverable)

    store = ManifestStore(archive)
    if not store.acquire(owner):
        print(json.dumps({"error": "档案被有效锁占用", "lock": str(store.lock_dir)}, ensure_ascii=False))
        return EXIT_USAGE

    def mutate(m: dict[str, Any]) -> None:
        m["revision"] = int(m.get("revision", 0)) + 1
        for item in m.get("assignments", []):
            if item.get("assignment_id") == assignment_id:
                item.setdefault("attempts", [])
                if ack:
                    item["attempts"].append({"profile": f"ack:{ack}", "argv_sha256": _sha256_str(ack or ""), "started_at": _now_iso(), "ended_at": _now_iso(),
                                             "detail": "问题级豁免引用（有效性由 check_audit_gate.py 校验）"})
                item["attempts"].extend(attempts)
                if succeeded:
                    item["status"] = "succeeded"
                    item["deliverable_sha256"] = _sha256_file(deliverable)
                else:
                    item["status"] = "blocked" if adjudicate(mode, gate, failure)["outcome"] == "blocked" else "failed"

    try:
        updated = store.update(mutate, owner)
    finally:
        store.release()

    if succeeded:
        print(json.dumps({"outcome": "succeeded", "tier": tier, "role": role, "mode": mode, "source": source,
                          "deliverable_sha256": _sha256_file(deliverable), "revision": updated["revision"]}, ensure_ascii=False))
        return EXIT_OK
    verdict = adjudicate(mode, gate, failure)
    print(json.dumps({"outcome": verdict["outcome"], "tier": tier, "role": role, "mode": f"{mode}（{mode_why}）", "gate": gate,
                      "failure_code": failure, "reason": verdict["reason"], "needs_external_review": verdict.get("needs_external_review", False),
                      "attempts": len(attempts)}, ensure_ascii=False))
    return verdict["exit"]


def cmd_resolve(cwd: Path) -> int:
    cwd = cwd.resolve()
    tier, archive, man = find_manifest(cwd)
    config = load_dispatch_config()
    out: dict[str, Any] = {"tier": tier, "archive": str(archive) if archive else None,
                           "config_present": config["present"], "default_dispatch_mode": config["default_dispatch_mode"] or "strict(硬默认)"}
    if man:
        errs = validate_manifest(man)
        out["manifest_errors"] = errs
        out["assignments"] = []
        for a in man.get("assignments", []):
            mode, why = resolve_mode(man, config, a["role"])
            out["assignments"].append({"id": a.get("assignment_id"), "role": a.get("role"), "status": a.get("status"),
                                       "gate": "hard" if a.get("role") in HARD_ROLES else "soft", "mode": f"{mode}（{why}）",
                                       "profile": a.get("command_profile")})
    else:
        out["note"] = "无第 1/2 级档案；第 3 级默认角色表生效（软角色 UNCONFIGURED→本地承载；硬门禁 blocked）"
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return EXIT_OK


# ---------------------------------------------------------------- 配置迁移（§3.4/§3.5）

def _tokenize_candidate(candidate: str) -> tuple[list[str], bool]:
    """保守分词：含引号/转义/不可判定字符 → (…)拒迁（人工仲裁 R3-01 B 收缩）。"""
    if any(ch in candidate for ch in "\"'\\") or not candidate.strip():
        return [], False
    return candidate.split(), True


def migrate_config(config_path: Path = WS_CONFIG, apply: bool = False) -> int:
    """幂等字段级迁移：仅缺失写入不覆盖；角色表自由命令 → 结构化 profiles（保守判定）。"""
    if not config_path.exists():
        print(json.dumps({"error": f"配置不存在: {config_path}"}, ensure_ascii=False))
        return EXIT_USAGE
    config = load_dispatch_config(config_path)
    text = config_path.read_text(encoding="utf-8")
    sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
    from setup_agents import parse_role_table  # noqa: PLC0415
    table = parse_role_table(text)

    actions: list[str] = []
    refusals: list[dict[str, str]] = []
    new_profiles: dict[str, Any] = {}
    for role, info in table.items():
        cmd = info.get("cmd", "")
        if any(k in cmd for k in ("subagent", "auto", "内置")):
            actions.append(f"{role}: 表声明内置 → 不生成 profile（SUBAGENT_AUTO）")
            continue
        candidates = [c.strip() for c in cmd.split("||") if c.strip()]
        names = [f"{role.lower()}-primary"] + ([f"{role.lower()}-fallback"] if len(candidates) > 1 else [])
        migrated_any = False
        for cand, pname in zip(candidates, names):
            if pname in (config.get("command_profiles") or {}):
                actions.append(f"{role}/{pname}: 已存在 → 不覆盖")
                migrated_any = True
                continue
            tokens, ok = _tokenize_candidate(cand)
            if not ok or not tokens or Path(tokens[0]).name not in KNOWN_EXEC_BASENAMES:
                refusals.append({"role": role, "candidate": cand,
                                 "reason": "含引号/转义或可执行族未知 → needs-manual-conversion（绝不静默改写 argv）"})
                continue
            if "-p" not in tokens and "--print" not in tokens and "--prompt" not in tokens:
                tokens = tokens + ["-p", PROMPT_PLACEHOLDER]
            new_profiles[pname] = {"argv": tokens, "timeout_s": 900,
                                   "note": f"migrated-from: {role} 表行；append: -p {{PROMPT}}"}
            actions.append(f"{role}/{pname}: ← `{' '.join(tokens)}`")
            migrated_any = True
        if len(candidates) > 1 and migrated_any and f"{role.lower()}-fallback" in new_profiles:
            new_profiles[f"{role.lower()}-primary"]["fallback_profile"] = f"{role.lower()}-fallback"

    merged_profiles = {**(config.get("command_profiles") or {}), **new_profiles}
    keys_needed: list[str] = []
    if not config["present"]:
        keys_needed = ["整块新增调度参数 yaml 块"]
    else:
        if config.get("default_dispatch_mode") is None:
            keys_needed.append("default_dispatch_mode: strict")
        if not config.get("command_profiles"):
            keys_needed.append(f"command_profiles（{len(new_profiles)} 个）")
        if config.get("dispatch_authorizations") is None and "dispatch_authorizations" not in text:
            keys_needed.append("dispatch_authorizations: []")

    print(json.dumps({"config": str(config_path), "actions": actions, "refusals": refusals,
                      "missing_keys": keys_needed, "new_profiles": new_profiles,
                      "mode": "apply" if apply else "dry-run（--apply 写入）"}, ensure_ascii=False, indent=1))
    if not apply:
        return EXIT_OK
    block = {
        "default_dispatch_mode": config.get("default_dispatch_mode") or "strict",
        "command_profiles": merged_profiles,
        "dispatch_authorizations": config.get("dispatch_authorizations") or [],
    }
    yaml_block = "```yaml\n" + yaml.safe_dump(block, allow_unicode=True, sort_keys=False) + "```\n"
    m = _DISPATCH_BLOCK_RE.search(text)
    if m:
        text = text[: m.start()] + yaml_block + text[m.end():]
    else:
        text = (text.rstrip() + "\n\n## 角色调度参数（三级解析第 3 级）\n\n"
                "> 契约见 `schemas/command_profile.schema.json` 与 `rules/角色协作.md`；由 `setup_agents.py --migrate-command-profiles` 迁移生成，亦可手工维护。\n\n" + yaml_block)
    fd, tmp = tempfile.mkstemp(dir=config_path.parent, prefix=f".{config_path.name}.tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, config_path)
    print(f"✅ 已原子写入 {config_path}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--migrate-config", action="store_true", help="第 3 级配置字段级迁移（默认 dry-run，--apply 写入；幂等不覆盖）")
    p.add_argument("--apply", action="store_true", help="配合 --migrate-config 实际写入")
    p.add_argument("--config", type=Path, default=WS_CONFIG, help="workspace-config.md 路径")
    sub = p.add_subparsers(dest="cmd")
    pr = sub.add_parser("resolve", help="解析三级链，打印生效指派")
    pr.add_argument("--cwd", type=Path, default=Path.cwd())
    pu = sub.add_parser("run", help="执行指派（矩阵裁决 + CAS 写回）")
    pu.add_argument("--cwd", type=Path, default=Path.cwd())
    pu.add_argument("--assignment", required=True)
    pu.add_argument("--prompt", help="任务提示词；缺省读 stdin")
    pu.add_argument("--ack", help="问题级豁免引用（hard blocked 时记录；有效性由 check_audit_gate.py 校验）")
    pu.add_argument("--owner", default=os.environ.get("USER", "manager"))
    pv = sub.add_parser("validate", help="校验 manifest 档案")
    pv.add_argument("--manifest", type=Path, required=True, help="capsule.yaml 或 workers.yaml")
    args = p.parse_args(argv)

    if args.migrate_config:
        return migrate_config(args.config, apply=args.apply)
    if args.cmd == "resolve":
        return cmd_resolve(args.cwd)
    if args.cmd == "validate":
        data = yaml.safe_load(args.manifest.read_text(encoding="utf-8")) or {}
        man = data.get("roles_manifest", data)
        errs = validate_manifest(man) if isinstance(man, dict) else ["manifest 非对象"]
        print(json.dumps({"file": str(args.manifest), "valid": not errs, "issues": errs}, ensure_ascii=False))
        return EXIT_OK if not errs else EXIT_USAGE
    if args.cmd == "run":
        prompt = args.prompt or (sys.stdin.read() if not sys.stdin.isatty() else "")
        if not prompt.strip():
            print(json.dumps({"error": "缺任务提示词（--prompt 或 stdin）"}, ensure_ascii=False))
            return EXIT_USAGE
        return run_assignment(args.cwd, args.assignment, prompt, args.ack, args.owner)
    p.print_help()
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
