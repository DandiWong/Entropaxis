#!/usr/bin/env python3
"""角色指派三级解析与强约束调度 · 唯一执行入口。

真源定义: rules/角色协作.md「角色指派三级解析与调度门禁」「调度优先序与降级契约」
机器契约: schemas/roles_manifest.schema.json + roles_config.schema.json（含 command_profile）
实施方案: data/docs/20260918_角色指派三级解析与强约束调度方案/01_方案.md (approved, revision 4)

设计不变量（违反任何一条即 fail-closed）:
  1. mode 单向收紧——档案级只允许 strict；graceful 仅第 3 级有效授权，越权/过期/含硬门禁角色一律按 strict;
  2. 档案零命令——命令本体只存第 3 级 command_profiles，档案只存 profile 标识；
  3. 调度五态与审计终态分离——无 waived，豁免仅问题级（经 check_audit_gate.py 校验）;
  4. 并发安全——目录锁 + revision CAS + os.replace 原子写；journal 仅追加审计轨迹，不作恢复载体
     （恢复源是档案自身，见方案 §2.5 人工仲裁 B 收缩）;
  5. 成功判据随产出形态——文档型看交付物写出、目录型另需状态围栏、代码型看 verify 退出码。
     一刀切的"文件存在"对后两类是假门禁（空目录曾一律判 succeeded）。

子命令:
  resolve  解析三级链与生效指派（含依据失效 stale 标记）
  run      执行指派：--assignment <既有ID> | --role <角色> --out <交付物>|--verify <校验profile>
  trace    人读执行轨迹（命令原文、退出码、耗时、输出尾部）
  validate 校验 manifest 档案

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
ROLES_CONFIG = SYSTEM_ROOT / "data" / "templates" / "roles.yaml"
# 旧布局：调度参数以 ```yaml 块寄居在 workspace-config.md。仅作只读兼容（未迁移的工作区）。
LEGACY_WS_CONFIG = SYSTEM_ROOT / "data" / "templates" / "workspace-config.md"
WS_CONFIG = ROLES_CONFIG
ACTIVE_CONFIG: Path | None = None  # 测试注入点；生产环境留空用 ROLES_CONFIG
SCHEMA_DIR = SYSTEM_ROOT / "schemas"

HARD_ROLES = {"Reviewer", "Maintainer"}
ALL_ROLES = ("Architecture", "Researcher", "Designer", "Builder", "Reviewer", "Maintainer", "Reporter")
SCHED_STATES = ("pending", "running", "succeeded", "failed", "blocked")
FAILURE_CODES = ("UNCONFIGURED", "SUBAGENT_AUTO", "NOT_EXECUTABLE", "TIMEOUT", "NO_VALID_OUTPUT", "CHAIN_EXHAUSTED", "VERIFY_FAILED")
# 聚合失败码优先级：最能说明「为什么没跑成」的排前
_FAILURE_PRIORITY = {"UNCONFIGURED": 0, "SUBAGENT_AUTO": 1, "NO_VALID_OUTPUT": 2, "TIMEOUT": 3, "NOT_EXECUTABLE": 4, "CHAIN_EXHAUSTED": 5, "VERIFY_FAILED": 6}
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


def _sha256_path(path: Path) -> str:
    """文件取内容指纹；目录取"相对路径 + 各文件指纹"的有序摘要。

    实现真源在 dispatch_receipt.content_sha256：档案指纹与回执指纹必须是同一个算法，
    各留一份只会漂移（方案 §4「能引用就不该复制」）。
    """
    return _receipt_module().content_sha256(path)


def _receipt_module():
    """按脚本/包两种运行方式加载 dispatch_receipt。"""
    try:
        from . import dispatch_receipt as mod  # type: ignore # noqa: PLC0415
    except ImportError:
        sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
        import dispatch_receipt as mod  # noqa: PLC0415
    return mod


# ---------------------------------------------------------------- 第 3 级配置

_DISPATCH_BLOCK_RE = re.compile(r"```yaml\n(.*?command_profiles:.*?)```", re.DOTALL)


def load_dispatch_config(config_path: Path | None = None) -> dict[str, Any]:
    """读第 3 级配置：roles.yaml 整文件（契约 schemas/roles_config.schema.json）；
    `.md` 路径按旧布局提取 ```yaml 块只读兼容。roles.yaml 缺失时回落旧布局。"""
    if config_path is None:
        config_path = ACTIVE_CONFIG or (ROLES_CONFIG if ROLES_CONFIG.exists() or not LEGACY_WS_CONFIG.exists() else LEGACY_WS_CONFIG)
    empty = {"default_dispatch_mode": None, "roles": {}, "command_profiles": {}, "dispatch_authorizations": [],
             "path": str(config_path), "present": False}
    if not config_path.exists():
        return empty
    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix in (".yaml", ".yml"):
        data = yaml.safe_load(text) or {}
    else:
        m = _DISPATCH_BLOCK_RE.search(text)
        if not m:
            return empty
        data = yaml.safe_load(m.group(1)) or {}
    return {
        "default_dispatch_mode": data.get("default_dispatch_mode"),
        "roles": data.get("roles") or {},
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


def find_archive_any(cwd: Path) -> tuple[int, Path | None]:
    """定位可建档的档案文件（含 assignments 仍为空的新建胶囊）。

    find_manifest 只认已有指派，新建胶囊的 `assignments: []` 会被判为第 3 级——直跑模式
    要往里写第一条指派，需要的正是这个"档案在、但还没指派"的状态。
    """
    for tier, name in ((1, "capsule.yaml"), (2, "workers.yaml")):
        for d in [cwd, *cwd.resolve().parents]:
            f = d / name
            if not f.exists():
                continue
            try:
                man = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("roles_manifest")
            except yaml.YAMLError:
                continue
            if isinstance(man, dict) and isinstance(man.get("revision"), int):
                return tier, f
    return 3, None


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
        a_allowed = {"assignment_id", "role", "command_profile", "target_path", "target_sha256", "deliverable",
                     "deliverable_ref", "verify_profile", "status", "attempts", "deliverable_sha256"}
        for k in a:
            if k not in a_allowed:
                errs.append(f"{where} 未知字段: {k}")
        # target_* 可缺省：调研/方案等无前序受审对象的角色，第一步就是凭空产出，没有可指纹的目标
        for req in ("assignment_id", "role", "command_profile", "status"):
            if req not in a:
                errs.append(f"{where} 缺必填字段: {req}")
        # 代码型角色的产出是一次修订而非一个文件，用 deliverable_ref 代替 deliverable
        if not a.get("deliverable") and not a.get("deliverable_ref"):
            errs.append(f"{where} 缺 deliverable 或 deliverable_ref（二者必居其一）")
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
        if a.get("status") == "succeeded" and a.get("deliverable") and not a.get("deliverable_sha256"):
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
    """第 3 级默认解析：roles.<角色>.profile 显式声明优先（null 即内置承载）；
    未声明时按约定名 `<角色>-primary`；旧布局再回落角色表（setup_agents.parse_role_table）。"""
    profiles = config.get("command_profiles") or {}
    declared = config.get("roles") or {}
    if role in declared:
        name = (declared[role] or {}).get("profile")
        if not name:
            return "SUBAGENT_AUTO", []
        return (name, resolve_profile_chain(name, config)) if name in profiles else ("UNCONFIGURED", [])
    canon = f"{role.lower()}-primary"
    if canon in profiles:
        return canon, resolve_profile_chain(canon, config)
    if not str(config.get("path", "")).endswith(".md"):
        return "UNCONFIGURED", []
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
    # 元字符只校验 argv 模板本身（禁 shell 字符串与 || 内联）。提示词是数据不是命令——
    # 全程 execvp 传参、无 shell，换行与引号无从注入；对它套同一张表会让任何多行提示词
    # 一律 NOT_EXECUTABLE，即"写了真实调研提示词就必定调度失败"。
    if not _argv_ok(argv):
        return None
    return [prompt if t == PROMPT_PLACEHOLDER else t for t in argv]


def _executable(argv0: str) -> str | None:
    if os.sep in argv0 or (os.altsep and os.altsep in argv0):
        p = Path(argv0)
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    return shutil.which(argv0)


STATE_FENCE = "deliverable-state"
_FENCE_RE = re.compile(r"```" + STATE_FENCE + r"\n(.*?)\n```", re.DOTALL)
FENCE_TEMPLATE = (
    '```' + STATE_FENCE + '\n{\n  "role": "<角色>",\n  "outputs": ["<本次产出的文件相对路径>"],\n'
    '  "self_check": ["<已自检项>"]\n}\n```'
)

# 七角色 × 成功判据的穷尽映射。缺映射即 fail-closed——此前 Maintainer 未被任何形态
# 覆盖，运行时只要求「有个标题行」，硬门禁角色实质无判据（审计 M-03）。
#   form:   doc=单文档 / any=文档或目录 / code=无交付物文件，判据交给 verify
#   schema: 交付物须通过 validate_schema（Front Matter + 正文结构判据）
#   fence:  须含该标签的机器状态围栏，且声明的产出经文件系统核验
#   gate:   须通过 check_audit_gate 的完整报告契约
#   doc_type: 交付物的 Front Matter type 必须等于该值。只穷尽角色键还不够——Maintainer
#            曾只有 {form, schema}，交一份结构合规的 Proposal 即判通过，硬门禁角色实
#            质仍无专属判据（复核 M-03）。绑定产出类型把这一类整体关掉，不止 Maintainer。
ROLE_CRITERIA: dict[str, dict[str, Any]] = {
    "Researcher":   {"form": "doc", "schema": True, "doc_type": "Research"},
    "Architecture": {"form": "doc", "schema": True, "doc_type": "Proposal"},
    "Designer":     {"form": "any", "schema": True, "fence": STATE_FENCE, "doc_type": "Proposal", "structure": "Design"},
    "Builder":      {"form": "code", "schema": True, "doc_type": "Spec"},
    "Reviewer":     {"form": "doc", "schema": True, "fence": "audit-state", "gate": True, "doc_type": "Audit"},
    "Maintainer":   {"form": "doc", "schema": True, "doc_type": "Report"},
    "Reporter":     {"form": "any", "schema": True, "fence": STATE_FENCE, "doc_type": "Report"},
}
FENCE_REQUIRED_ROLES = {r for r, c in ROLE_CRITERIA.items() if c.get("fence") == STATE_FENCE}
# 校验判据只归代码型角色。放开给所有角色时，`--role Reviewer --verify <任一 exit 0>`
# 能在无任何交付物的情况下写下 status: succeeded——硬门禁被整条绕过（审计 C-01）。
VERIFY_ROLES = {r for r, c in ROLE_CRITERIA.items() if c.get("form") == "code"}


def _fence_body(text: str, label: str) -> tuple[str | None, int]:
    """返回（围栏内容, 出现次数）。多于一个即歧义，调用方按不合规处理。"""
    pattern = _FENCE_RE if label == STATE_FENCE else re.compile(r"```" + re.escape(label) + r"\n(.*?)\n```", re.DOTALL)
    found = pattern.findall(text)
    return (found[0] if found else None), len(found)


def _fence_carrier_file(path: Path) -> Path | None:
    """围栏住在哪个文件里：单文件即自身；目录优先 README.md，否则首个 .md。"""
    if path.is_file():
        return path
    readme = path / "README.md"
    if readme.is_file():
        return readme
    return next((p for p in sorted(path.rglob("*.md")) if p.is_file()), None)


def parse_state_fence(path: Path, label: str = STATE_FENCE) -> dict[str, Any] | None:
    """读取机器状态围栏。机器可判字段与人读叙事物理分离——这条是 audit_report 第 12 轮
    重构的结论（散文解析的格式变体攻击面无界），此处把同一形态推广到其他角色。"""
    target = _fence_carrier_file(path)
    if target is None:
        return None
    body, count = _fence_body(target.read_text(encoding="utf-8", errors="replace"), label)
    if body is None or count != 1:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def validate_state_fence(path: Path, role: str) -> list[str]:
    """校验 deliverable-state 围栏并把声明的产出绑到文件系统事实上。

    只检查「是个对象且 outputs 非空」时，`{"outputs":["missing.html"]}` 配任一文件即
    判有效——声明与事实之间没有绑定，围栏就只是一段自述（审计 M-02）。
    """
    container = path if path.is_dir() else path.parent
    carrier = _fence_carrier_file(path)
    if carrier is None:
        return [f"未找到承载围栏的 Markdown 文件（目录型交付物需 README.md）: {path}"]
    body, count = _fence_body(carrier.read_text(encoding="utf-8", errors="replace"), STATE_FENCE)
    if count == 0:
        return [f"缺少 ```{STATE_FENCE}``` 机器状态围栏: {carrier.name}"]
    if count > 1:
        return [f"{carrier.name} 含 {count} 个 {STATE_FENCE} 围栏；须恰好一个，多个即状态歧义"]
    try:
        state = json.loads(body or "")
    except json.JSONDecodeError as exc:
        return [f"{carrier.name} 的围栏不是合法 JSON: {exc}"]
    try:
        from . import validate_schema as vs  # type: ignore # noqa: PLC0415
    except ImportError:
        sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
        import validate_schema as vs  # noqa: PLC0415
    errs = [f"围栏 {e}" for e in vs.validate(state, vs.load_schema("deliverable_state"))]
    if errs:
        return errs
    if state.get("role") != role:
        return [f"围栏 role={state.get('role')!r} 与调度角色 {role!r} 不符"]
    # 不变量是「声明的产出确实是胶囊内一个真实非空文件」，不是「写对了某个基准」。
    # 实测三种基准都有人写：容器相对、胶囊相对、工作区相对——只认一种会把合规产出
    # 判成造假。故按三个基准尝试解析，但最终路径一律必须落在胶囊内，越界照拦。
    capsule = next((d for d in [container, *container.parents] if (d / "capsule.yaml").is_file()), container)
    for rel in state["outputs"]:
        candidates = [(container / rel).resolve(), (capsule / rel).resolve(), (paths.WORKSPACE_ROOT / rel).resolve()]
        inside = [c for c in candidates if _within(c, capsule)]
        if not inside:
            return [f"围栏声明的产出越出胶囊目录: {rel}"]
        if not any(c.is_file() and c.stat().st_size > 0 for c in inside):
            return [f"围栏声明的产出不存在或为空: {rel}（相对 {container} 或 {capsule}）"]
    return []


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _doc_type_issues(path: Path, role: str, expected: str) -> list[str]:
    """角色的产出类型必须对得上：验收报告不能是一份方案，审计报告不能是一份调研。"""
    try:
        from . import validate_schema as vs  # type: ignore # noqa: PLC0415
    except ImportError:
        sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
        import validate_schema as vs  # noqa: PLC0415
    # 目录型交付物的主文档与围栏承载文件是同一个（README.md，否则首个 .md）。此前只认
    # README.md，无 README 的目录直接返回空——交一个 type: Proposal 的 note.md 配有效
    # 围栏即通过 Reporter 判据，doc_type 对整个目录型形态形同虚设（复核 M-03）。
    doc = path if path.is_file() else _fence_carrier_file(path)
    if doc is None or not doc.is_file() or doc.suffix != ".md":
        return []  # 目录内无任何 Markdown：无处判定类型，由围栏/form 判据拦
    fm = vs.parse_front_matter(doc.read_text(encoding="utf-8", errors="replace"))
    if fm is None:
        return [f"{doc.name} 缺 Front Matter，无法判定产出类型"]
    if fm.get("type") != expected:
        return [f"{role} 的产出类型须为 {expected}，实得 {fm.get('type')!r}（{doc.name}）"]
    return []


def _schema_issues(path: Path, rule_key: str | None = None) -> list[str]:
    """交付物须通过 Front Matter 与正文结构判据。

    结构 schema 此前是离线校验器的死路径——调度只看标题行，方案承诺的「合规写出」
    从未在成功状态转换处求值（审计 M-01）。
    """
    try:
        from . import validate_schema as vs  # type: ignore # noqa: PLC0415
    except ImportError:
        sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
        import validate_schema as vs  # noqa: PLC0415
    targets = [path] if path.is_file() else [p for p in sorted(path.rglob("*.md")) if p.is_file()]
    issues: list[str] = []
    for t in targets:
        if vs.parse_front_matter(t.read_text(encoding="utf-8", errors="replace")) is None:
            continue  # 容器内的附属说明可以没有档头；主交付物缺档头由 form 检查拦
        issues += vs.check_markdown(t, rule_key=rule_key)
    return issues


def _audit_gate_issues(path: Path) -> list[str]:
    """Reviewer 的成功须通过完整审计报告契约，不只是「围栏能解析」。

    围栏存在性与 audit-state schema、外置特权、目标指纹的语义校验此前被拆开，
    却没有在成功状态转换处汇合——`{"arbitrary":true}` 即判有效（审计 C-02）。
    """
    try:
        from . import check_audit_gate as gate  # type: ignore # noqa: PLC0415
    except ImportError:
        sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
        import check_audit_gate as gate  # noqa: PLC0415
    # 签发前不查回执：回执待交付物过判据后才签发，此刻要求 receipt_id 是循环依赖。
    # 受审指纹绑定则必须在此刻查——过期指纹意味着复核的是旧版对象。
    return gate.check_report_file(path, receipt_check=False)


def deliverable_valid(path: Path, role: str | None = None) -> bool:
    """成功判据按角色取。要拿不合格原因用 _deliverable_issues——返回类型不随入参变，
    否则 `assertFalse(deliverable_valid(...))` 这类调用会在元组上静默取真。"""
    return not _deliverable_issues(path, role)


def _deliverable_issues(path: Path, role: str | None) -> list[str]:
    if not path.exists():
        return [f"交付物不存在: {path}"]
    if role is not None and role not in ROLE_CRITERIA:
        return [f"角色 {role!r} 无成功判据映射；缺映射一律 fail-closed，不得按缺省文档分支放宽"]
    crit = ROLE_CRITERIA.get(role or "", {})
    form = crit.get("form", "any")

    if path.is_dir():
        if form == "doc":
            return [f"{role} 的交付物须为单个文档，实得目录: {path}"]
        if not any(p.is_file() for p in path.rglob("*")):
            return [f"目录为空，不构成产出: {path}"]
    else:
        if path.stat().st_size == 0:
            return [f"交付物为空文件: {path}"]
        if path.suffix == ".md" and not any(
            re.match(r"^#{1,6} ", ln) for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
        ):
            return [f"Markdown 无任何标题行: {path.name}"]

    issues: list[str] = []
    if crit.get("doc_type"):
        issues += _doc_type_issues(path, role or "", crit["doc_type"])
    if crit.get("schema"):
        issues += _schema_issues(path, crit.get("structure"))
    if crit.get("fence") == STATE_FENCE:
        issues += validate_state_fence(path, role or "")
    elif crit.get("fence"):
        if parse_state_fence(path, crit["fence"]) is None:
            issues.append(f"缺少可解析的 ```{crit['fence']}``` 围栏（须恰好一个、内为合法 JSON 对象）")
    if crit.get("gate") and path.is_file():
        issues += [f"审计契约: {e}" for e in _audit_gate_issues(path)]
    return issues


_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def stamp_carrier(path: Path, carrier: str | None, carrier_ref: str | None = "",
                  fallback_reason: str | None = "", audit: bool = False,
                  receipt_id: str | None = "") -> bool:
    """把实际承载三元组写回 .md 交付物 Front Matter。

    与 audit_report.schema.json 的 reviewer_mode/reviewer_ref/fallback_reason 同构（该文件
    是本三元组的 Audit 特化形态，两者结构由 tests 机械钉死防漂移）。值来自调度回执而非模型
    自述。

    三种取值语义：非空=写入，`""`=删除该字段，**`None`=不动该字段**。`None` 供失败路径使用：
    没有回执就没有承载事实，不能断言它是谁产出的。此前失败一律盖 `session-local`，把一份
    真由外置 CLI 产出的报告标成会话内自评（复核 B），更早一版还会连带抹掉上一轮的有效回执
    （复核 M-07）——失败只写 `fallback_reason`，承载归承载，两件事不混。
    """
    if path.is_dir():
        # 目录型交付物（Reporter 的汇报容器）本身没有 Front Matter，盖在其 README.md 上；
        # 没有就不盖——档案里仍有承载记录，但产物自身无处可标，如实返回 False。
        readme = path / "README.md"
        return stamp_carrier(readme, carrier, carrier_ref, fallback_reason, audit, receipt_id) if readme.is_file() else False
    if path.suffix != ".md" or not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return False
    block = m.group(1)
    fields = [("carrier", carrier), ("carrier_ref", carrier_ref), ("fallback_reason", fallback_reason),
              ("receipt_id", receipt_id)]
    if audit:
        # 承载信息一并接管 Audit 的特化字段：模型无从知道自己是不是被外置派发的——
        # 实测外置 Reviewer 自述 `reviewer_mode: session`（从它自己看就是个会话）。
        # 让模型写承载等于让它猜，猜错还会触发独立性冲突告警。
        fields += [("reviewer_mode", None if carrier is None else ("external" if carrier != "session-local" else "session")),
                   ("reviewer_ref", carrier_ref)]
    for key, value in fields:
        if value is None:
            continue  # 不动：本次调度对该字段没有可断言的事实
        if not value:
            block = re.sub(rf"^{key}:.*\n?", "", block, flags=re.MULTILINE).rstrip()
            continue
        line = f"{key}: {_yaml_inline(value)}"
        block = (re.sub(rf"^{key}:.*$", line, block, count=1, flags=re.MULTILINE)
                 if re.search(rf"^{key}:", block, re.MULTILINE) else block + "\n" + line)
    path.write_text(f"---\n{block}\n---\n{text[m.end():]}", encoding="utf-8")
    return True


def _yaml_inline(value: str) -> str:
    """单行 YAML 标量：命令原文含冒号/引号，裸写会让 Front Matter 解析错位。"""
    return json.dumps(value, ensure_ascii=False) if re.search(r'[:#\'"\n{}\[\]]', value) else value


TRACE_LOG = SYSTEM_ROOT / "data" / "logs" / "dispatch-trace.jsonl"
TRACE_MAX_BYTES = 4 * 1024 * 1024


def trace(record: dict[str, Any], path: Path | None = None) -> None:
    """追加一条可读执行轨迹（argv 原文 + 输出尾部）。

    档案只存 argv_sha256——可证伪，不可还原。人要回答"这条命令到底跑没跑、跑的是什么"，
    需要命令原文。原文含提示词全文，故只落在 data/（不随版本库分发），不进胶囊档案。
    失败一律吞掉：留痕不得反过来阻断被留痕的动作。
    """
    log = path or TRACE_LOG
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        # 一行约 2KB（提示词 + 输出尾部），无上限会无声长成几十 MB；满了就整体转存
        # 为单个 .1 备份再从头写，不做多代归档——轨迹是排查用的近期证据，不是台账。
        if log.is_file() and log.stat().st_size > TRACE_MAX_BYTES:
            os.replace(log, log.with_suffix(log.suffix + ".1"))
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _now_iso(), **record}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def execute_chain(chain: list[dict[str, Any]], prompt: str, cwd: Path, deliverable: Path,
                  role: str | None = None) -> tuple[str, list[dict[str, Any]], bool]:
    """执行备选链，返回 (聚合失败码, attempts, succeeded)。"""
    attempts: list[dict[str, Any]] = []
    codes: set[str] = set()
    for prof in chain:
        started = _now_iso()
        argv = build_argv(prof, prompt)
        if argv is None:
            attempts.append({"profile": prof["name"], "argv_sha256": _sha256_str(json.dumps(prof.get("argv"))), "started_at": started,
                             "ended_at": _now_iso(), "failure_code": "NOT_EXECUTABLE", "detail": "argv 缺 {PROMPT} 占位符或含 shell 元字符"})
            trace({"role": role, "profile": prof["name"], "argv": prof.get("argv"), "executed": False,
                   "failure_code": "NOT_EXECUTABLE", "detail": "argv 缺 {PROMPT} 占位符或含 shell 元字符"})
            codes.add("NOT_EXECUTABLE")
            continue
        exe = _executable(argv[0])
        if exe is None:
            attempts.append({"profile": prof["name"], "argv_sha256": _sha256_str(json.dumps(argv)), "started_at": started,
                             "ended_at": _now_iso(), "failure_code": "NOT_EXECUTABLE", "detail": f"{argv[0]!r} 不可执行"})
            trace({"role": role, "profile": prof["name"], "argv": argv, "executed": False,
                   "failure_code": "NOT_EXECUTABLE", "detail": f"{argv[0]!r} 不可执行"})
            codes.add("NOT_EXECUTABLE")
            continue
        t0 = time.time()
        try:
            # stdin 必须显式给 DEVNULL：不给就继承调用者的 stdin，而被调度的 CLI 见 stdin
            # 非 tty 会当成"有管道输入"并读到 EOF 为止——调用者不关闭就永远读不到。实测
            # 外置 Reviewer 卡在 `phase: readPipedInput` 22 分钟零产出，最后以 TIMEOUT 收场，
            # 看起来像模型不行，其实一个字都没开始生成。
            proc = subprocess.run([exe, *argv[1:]], cwd=str(cwd), capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=int(prof.get("timeout_s", 900)))
            ended = _now_iso()
            if proc.returncode == 0 and deliverable != cwd:
                # 判据要求的承载四字段只有盖章能提供，而盖章此前排在判据之后——被调度的
                # 模型要么猜（方案 §3.3 明说不该让它猜），要么照抄上一轮的旧承载字段才能
                # 过判据；实测第 5 轮 Reviewer 按要求不写这些字段，反被判 NO_VALID_OUTPUT
                # （复核 A）。退出码为 0 时承载事实已经确定——就是这条 profile 产出的——
                # 先盖上，判据于是只评角色自己该产出的内容。receipt_id 清空，等判据过了再签。
                stamp_carrier(deliverable, prof["name"],
                              " ".join(prof.get("argv") or []) or prof["name"],
                              "", audit=(role == "Reviewer"), receipt_id="")
            reasons = _deliverable_issues(deliverable, role)
            ok = proc.returncode == 0 and not reasons
            code = "" if ok else ("NO_VALID_OUTPUT" if proc.returncode == 0 else "CHAIN_EXHAUSTED")
            trace({"role": role, "profile": prof["name"], "argv": argv, "cwd": str(cwd), "executed": True,
                   "exit_code": proc.returncode, "duration_s": round(time.time() - t0, 1),
                   "deliverable": str(deliverable), "deliverable_valid": not reasons,
                   # 判据不合格要说出是哪一条：只报 NO_VALID_OUTPUT 会让人以为是模型不行
                   "criteria_issues": reasons[:8] or None,
                   "failure_code": code or None, "stdout_tail": (proc.stdout or "")[-600:],
                   "stderr_tail": (proc.stderr or "")[-600:]})
            if ok:
                attempts.append({"profile": prof["name"], "argv_sha256": _sha256_str(json.dumps(argv)), "started_at": started,
                                 "ended_at": ended, "exit_code": 0, "report_sha256": _sha256_path(deliverable)})
                return "", attempts, True
            attempts.append({"profile": prof["name"], "argv_sha256": _sha256_str(json.dumps(argv)), "started_at": started,
                             "ended_at": ended, "exit_code": proc.returncode, "failure_code": code,
                             "detail": ("；".join(reasons) or (proc.stderr or ""))[-400:]})
            codes.add(code)
        except subprocess.TimeoutExpired:
            attempts.append({"profile": prof["name"], "argv_sha256": _sha256_str(json.dumps(argv)), "started_at": started,
                             "ended_at": _now_iso(), "failure_code": "TIMEOUT", "timeout": True})
            trace({"role": role, "profile": prof["name"], "argv": argv, "cwd": str(cwd), "executed": True,
                   "failure_code": "TIMEOUT", "duration_s": round(time.time() - t0, 1)})
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
    target_file: Path | None = None
    if a.get("target_path"):
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
            failure, attempts, succeeded = execute_chain(chain, prompt, cwd, deliverable, role)
    else:
        failure, attempts, succeeded = execute_chain(chain, prompt, cwd, deliverable, role)

    carrier = next((x["profile"] for x in reversed(attempts) if "failure_code" not in x), "session-local") if succeeded else "session-local"
    carrier_ref = _profile_command_text(carrier, config) if carrier != "session-local" else "当前会话 Agent"
    # 档案声明了 target_path 就必须把它绑进回执，否则同一份报告走 run 与 run_direct
    # 两条路径会得到"绑定/未绑定受审对象"两种回执，门禁的 C-03 校验随入口而变。
    receipt = _issue_receipt(role, attempts, deliverable, target_file, carrier, carrier_ref) if succeeded else {}
    # 先盖章再取指纹。失败时承载三项一律传 None（不动）：没有回执就没有承载事实。
    stamped = stamp_carrier(deliverable,
                            carrier if succeeded else None,
                            carrier_ref if succeeded else None,
                            "" if succeeded else _FAILURE_TO_REASON.get(failure, ""),
                            audit=(role == "Reviewer"),
                            receipt_id=receipt.get("receipt_id", "") if succeeded else None)

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
                    item["deliverable_sha256"] = _sha256_path(deliverable)
                else:
                    item["status"] = "blocked" if adjudicate(mode, gate, failure)["outcome"] == "blocked" else "failed"

    try:
        updated = store.update(mutate, owner)
    finally:
        store.release()

    if succeeded:
        print(json.dumps({"outcome": "succeeded", "tier": tier, "role": role, "mode": mode, "source": source,
                          "carrier": carrier, "carrier_ref": carrier_ref, "carrier_stamped": stamped,
               "receipt_id": receipt.get("receipt_id") or None, "receipt_signed": receipt.get("mac", "") not in ("", "unsigned"),
                          "deliverable_sha256": _sha256_path(deliverable), "revision": updated["revision"]}, ensure_ascii=False))
        return EXIT_OK
    verdict = adjudicate(mode, gate, failure)
    print(json.dumps({"outcome": verdict["outcome"], "tier": tier, "role": role, "mode": f"{mode}（{mode_why}）", "gate": gate,
                      "failure_code": failure, "reason": verdict["reason"], "needs_external_review": verdict.get("needs_external_review", False),
                      "attempts": len(attempts)}, ensure_ascii=False))
    return verdict["exit"]


def run_direct(cwd: Path, role: str, prompt: str, out: Path | None, owner: str,
               target: Path | None = None, verify: str | None = None) -> int:
    """直跑模式：第 3 级解析 → 执行 → 判据裁定 → 指派记录由工具落笔。

    此前调度一次必须先手写 assignment YAML 并算 target_sha256，而调研/方案类角色根本
    没有前序受审对象——建档成本高到规则拧不过摩擦力，外置 CLI 事实上从不被调用。建档
    要求不取消，改由工具写（capsule.yaml 注释早已声明 assignments "由 dispatch_role.py
    写入/更新"）；档案不存在时只打回执，不擅自造容器。

    成功判据按角色产出形态取：文档型看交付物是否合规写出；目录型另需机器状态围栏；
    代码型（verify）以校验命令退出码为准——"文件存在"对 Builder 毫无意义。
    """
    cwd = cwd.resolve()
    if verify and role not in VERIFY_ROLES:
        print(json.dumps({"error": f"--verify 只适用于 {sorted(VERIFY_ROLES)}，实得 {role}"}, ensure_ascii=False))
        return EXIT_USAGE
    if out is None and role in HARD_ROLES:
        print(json.dumps({"error": f"硬门禁角色 {role} 无交付物不得调度"}, ensure_ascii=False))
        return EXIT_USAGE
    deliverable = (out if out is None or out.is_absolute() else cwd / out)
    deliverable = deliverable.resolve() if deliverable is not None else None
    target_file = (target if target is None or target.is_absolute() else cwd / target)
    target_file = target_file.resolve() if target_file is not None else None
    if target_file is not None and not target_file.exists():
        print(json.dumps({"error": f"--target 不存在: {target_file}"}, ensure_ascii=False))
        return EXIT_USAGE
    config = load_dispatch_config()
    mode, mode_why = resolve_mode(None, config, role)
    gate = "hard" if role in HARD_ROLES else "soft"
    source, chain = _tier3_default_chain(role, config)

    if chain:
        bound = _bind_prompt(prompt, role, deliverable, target_file, cwd)
        if deliverable is None:
            # 无交付物文件（代码型）：先让外置 CLI 跑完，判据交给 verify
            failure, attempts, succeeded = execute_chain(chain, bound, cwd, cwd, role)
        else:
            failure, attempts, succeeded = execute_chain(chain, bound, cwd, deliverable, role)
    else:
        failure, attempts, succeeded = source, [], False

    verify_result: dict[str, Any] | None = None
    if verify:
        verify_result = run_verify(verify, config, cwd, role)
        succeeded = bool(verify_result["passed"])
        if not succeeded:
            failure = verify_result["failure_code"]
        else:
            failure = ""
        attempts.append({"profile": f"verify:{verify}", "argv_sha256": verify_result["argv_sha256"],
                         "started_at": verify_result["started_at"], "ended_at": verify_result["ended_at"],
                         "exit_code": verify_result.get("exit_code"),
                         **({} if succeeded else {"failure_code": failure, "detail": verify_result.get("detail", "")[-400:]})})

    ok_attempt = next((a for a in reversed(attempts) if "failure_code" not in a and not a["profile"].startswith("verify:")), None)
    carrier = ok_attempt["profile"] if (succeeded and ok_attempt) else "session-local"
    carrier_ref = _profile_command_text(carrier, config) if carrier != "session-local" else "当前会话 Agent"
    fallback_reason = "" if succeeded else _FAILURE_TO_REASON.get(failure, "")
    verdict = {"outcome": "succeeded", "exit": EXIT_OK} if succeeded else adjudicate(mode, gate, failure)
    status = "succeeded" if succeeded else ("blocked" if verdict["outcome"] == "blocked" else "failed")
    # 先盖章再取指纹，否则档案记的是盖章前的文件
    receipt = _issue_receipt(role, attempts, deliverable, target_file, carrier, carrier_ref) if succeeded else {}
    stamped = (stamp_carrier(deliverable,
                             carrier if succeeded else None,
                             carrier_ref if succeeded else None,
                             fallback_reason,
                             audit=(role == "Reviewer"),
                             receipt_id=receipt.get("receipt_id", "") if succeeded else None)
               if deliverable is not None else False)

    tier, archive = find_archive_any(cwd)
    archived = False
    if archive is not None:
        key = str(deliverable) if deliverable is not None else f"{role}:{verify or ''}:{target_file}"
        aid = f"{role}-{_sha256_str(key)[:8]}"  # 同一交付物重跑复用同一 ID，禁重编号
        store = ManifestStore(archive)
        if store.acquire(owner):
            def mutate(m: dict[str, Any]) -> None:
                m["revision"] = int(m.get("revision", 0)) + 1
                m.setdefault("assignments", [])
                rec = next((x for x in m["assignments"] if x.get("assignment_id") == aid), None)
                if rec is None:
                    rec = {"assignment_id": aid, "role": role,
                           "command_profile": source if chain else f"{role.lower()}-primary",
                           "status": status, "attempts": []}
                    m["assignments"].append(rec)
                else:
                    rec["status"] = status
                    rec.setdefault("attempts", [])
                if deliverable is not None:
                    rec["deliverable"] = os.path.relpath(deliverable, archive.parent)
                if verify:
                    # 代码型产出散在多文件，修订标识与文档交付物并存而非二选一
                    rec["verify_profile"] = verify
                    rec["deliverable_ref"] = _workspace_ref(cwd)
                elif deliverable is None:
                    rec["deliverable_ref"] = _workspace_ref(cwd)
                if target_file is not None:
                    # 依据指纹：依据变了，既有 succeeded 即失效（resolve 会标 stale）
                    rec["target_path"] = os.path.relpath(target_file, archive.parent)
                    rec["target_sha256"] = _sha256_file(target_file)
                if target_file is not None:
                    # 依据指纹随每次执行留痕：assignment 上的 target_* 只反映最新一跳，
                    # 「这版方案当初基于哪份调研」必须在执行记录里查得到。
                    for at in attempts:
                        at.setdefault("target_sha256", _sha256_file(target_file))
                rec["attempts"].extend(attempts)
                if succeeded and deliverable is not None:
                    rec["deliverable_sha256"] = _sha256_path(deliverable)
            try:
                store.update(mutate, owner)
                archived = True
            finally:
                store.release()

    receipt = {"outcome": verdict["outcome"], "role": role, "gate": gate, "mode": f"{mode}（{mode_why}）",
               "carrier": carrier, "carrier_ref": carrier_ref, "carrier_stamped": stamped,
               "receipt_id": receipt.get("receipt_id") or None, "receipt_signed": receipt.get("mac", "") not in ("", "unsigned"),
               "deliverable": str(deliverable) if deliverable else None,
               "target": str(target_file) if target_file else None,
               "archived": archived, "tier": tier, "failure_code": failure or None,
               "fallback_reason": fallback_reason or None, "reason": verdict.get("reason", ""),
               "needs_external_review": verdict.get("needs_external_review", False), "attempts": len(attempts)}
    if verify_result is not None:
        receipt["verify"] = {"profile": verify, "passed": verify_result["passed"], "exit_code": verify_result.get("exit_code")}
    print(json.dumps(receipt, ensure_ascii=False))
    return verdict["exit"]



def _issue_receipt(role: str, attempts: list[dict[str, Any]], deliverable: Path | None,
                   target: Path | None, carrier: str, carrier_ref: str) -> dict[str, Any]:
    """成功后签发调度回执（信任根在工作目录之外，被调度角色的 --cwd 够不着）。

    交付物 Front Matter 与模型同权限可编辑，盖在里面的承载字段不构成第三方可核验的证据
    （审计 C-03）。回执由签发者用本机密钥做 MAC，绑定角色 + argv + 交付物 + 依据四项指纹；
    交付物改一个字节即失配。签发失败不阻断调度，但也不伪装成已验证。
    """
    dr_receipt = _receipt_module()
    ok_attempt = next((a for a in reversed(attempts) if "failure_code" not in a), None)
    argv_sha = ok_attempt.get("argv_sha256", "") if ok_attempt else ""
    try:
        return dr_receipt.issue(role, argv_sha, deliverable, target, carrier, carrier_ref)
    except OSError:
        return {}

def _workspace_ref(cwd: Path) -> str:
    """代码型交付物的标识：git 修订号；非 git 目录退回路径。"""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(cwd), capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return f"git:{r.stdout.strip()}"
    except (OSError, subprocess.SubprocessError):
        pass
    return f"path:{cwd}"


def _profile_command_text(profile_name: str, config: dict[str, Any]) -> str:
    """profile 名 → 命令原文（档案零命令：原文只在第 3 级，这里仅用于回执与盖章）。"""
    prof = (config.get("command_profiles") or {}).get(profile_name) or {}
    argv = prof.get("argv") or []
    return " ".join(argv) if argv else profile_name


def _bind_prompt(prompt: str, role: str, deliverable: Path | None, target: Path | None, cwd: Path) -> str:
    """把工具侧才知道的交付约定绑进提示词。

    外置 CLI 默认只往 stdout 打印，不绑落盘路径则每次调度都以 NO_VALID_OUTPUT 收场；
    目录型角色另需状态围栏，否则"目录存在"就成了假门禁。这些约定由工具补，不依赖
    调用者每次记得写。
    """
    parts = [prompt, ""]
    if target is not None:
        parts.append(f"[输入依据] 先读取并严格依据该文件：{target}")
    if deliverable is not None:
        parts.append(f"[交付约定] 将本次产出写入：{deliverable}（覆盖写，保留原 Front Matter 字段）。")
    else:
        parts.append(f"[交付约定] 在工作目录 {cwd} 内直接改写代码与测试文件；不要只输出到终端。")
    # 判据要求什么，提示词就得说什么——否则角色因不知情而失败，看起来像模型不行
    fence = ROLE_CRITERIA.get(role, {}).get("fence")
    if fence == STATE_FENCE:
        base = deliverable if deliverable and deliverable.is_dir() else (deliverable.parent if deliverable else cwd)
        parts.append(f"[状态围栏] 交付物中必须恰好包含一个机器可读围栏，role 与本角色一致，outputs 逐项为"
                     f"真实存在的文件路径（相对 {base}，会被逐一核验，写不存在的文件即判不合规）：\n" + FENCE_TEMPLATE)
    elif fence:
        parts.append(f"[状态围栏] 交付物中必须恰好包含一个 ```{fence}``` 围栏，内为合法 JSON "
                     '（如 {"issues": [{"id": "C-1", "level": "Critical", "status": "open"}], "critical_acks": []}）；'
                     "散文清单不被解析器读取。")
    if ROLE_CRITERIA.get(role, {}).get("schema"):
        parts.append("[结构判据] 交付物须通过 validate_schema.py：Front Matter 字段合规，且正文含本类型"
                     "要求的章节（调研四段式并标注带链接的一手信源；方案含边界与替代方案对比；报告含"
                     "检查对象/证据/结论/未覆盖范围）。")
    return "\n\n".join(parts)


_FAILURE_TO_REASON = {
    "UNCONFIGURED": "not_configured", "SUBAGENT_AUTO": "not_configured",
    "NOT_EXECUTABLE": "not_executable", "TIMEOUT": "timeout",
    "NO_VALID_OUTPUT": "no_valid_output", "CHAIN_EXHAUSTED": "launch_failed",
    "VERIFY_FAILED": "no_valid_output",
}


# 校验期间必然变动、与被审代码无关的噪声目录。
# ponytail: 固定排除表，够用即止；真出现别的构建缓存再加，不为此引入 .gitignore 解析。
_VERIFY_NOISE_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                      "node_modules", ".venv", "venv", ".tox", "htmlcov", ".coverage"}


def _source_fingerprints(root: Path) -> dict[str, str]:
    """校验前的既有文件指纹表（排除噪声目录）。"""
    out: dict[str, str] = {}
    for p in root.rglob("*"):
        if not p.is_file() or _VERIFY_NOISE_DIRS & set(p.relative_to(root).parts):
            continue
        try:
            out[str(p.relative_to(root))] = _sha256_file(p)
        except OSError:
            continue
    return out


def _tampered_paths(before: dict[str, str], root: Path) -> list[str]:
    """校验后被改写或删除的既有文件；新建文件不算（缓存、报告属正常产物）。"""
    changed = []
    for rel, sha in before.items():
        p = root / rel
        if not p.is_file():
            changed.append(f"{rel}（已删除）")
        elif _sha256_file(p) != sha:
            changed.append(rel)
    return sorted(changed)


def run_verify(profile_name: str, config: dict[str, Any], cwd: Path, role: str) -> dict[str, Any]:
    """执行校验 profile：Builder 的成功判据是校验命令退出码，不是文件是否存在。

    这是全角色里唯一机器可判的客观信号——它得真让测试绿。但退出码只证明"这条命令通过了
    被审方自带的测试"，不证明测试充分：测试与被审代码同源，弱测试照样绿（审计 M-04，仍
    open）。校验 profile 复用同一套
    command_profiles 结构，不引入第二种命令载体；不含 {PROMPT} 占位符（校验不接受任务文本）。
    """
    started = _now_iso()
    prof = (config.get("command_profiles") or {}).get(profile_name)
    if not isinstance(prof, dict) or not _argv_ok(list(prof.get("argv") or [])):
        trace({"role": role, "profile": f"verify:{profile_name}", "executed": False, "failure_code": "UNCONFIGURED"})
        return {"passed": False, "failure_code": "UNCONFIGURED", "started_at": started, "ended_at": _now_iso(),
                "argv_sha256": _sha256_str(profile_name), "detail": f"校验 profile 未配置或含 shell 元字符: {profile_name}"}
    argv = list(prof["argv"])
    exe = _executable(argv[0])
    if exe is None:
        trace({"role": role, "profile": f"verify:{profile_name}", "argv": argv, "executed": False, "failure_code": "NOT_EXECUTABLE"})
        return {"passed": False, "failure_code": "NOT_EXECUTABLE", "started_at": started, "ended_at": _now_iso(),
                "argv_sha256": _sha256_str(json.dumps(argv)), "detail": f"{argv[0]!r} 不可执行"}
    t0 = time.time()
    before = _source_fingerprints(cwd)
    try:
        proc = subprocess.run([exe, *argv[1:]], cwd=str(cwd), capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=int(prof.get("timeout_s", 900)))
    except subprocess.TimeoutExpired:
        trace({"role": role, "profile": f"verify:{profile_name}", "argv": argv, "cwd": str(cwd), "executed": True,
               "failure_code": "TIMEOUT", "duration_s": round(time.time() - t0, 1)})
        return {"passed": False, "failure_code": "TIMEOUT", "started_at": started, "ended_at": _now_iso(),
                "argv_sha256": _sha256_str(json.dumps(argv)), "detail": "校验命令超时"}
    tampered = _tampered_paths(before, cwd)
    if tampered:
        # 校验跑在可写 cwd 里，被审方连产品代码带测试都能在验收阶段改——实测 profile 先把
        # product.py 改成语法错误再 exit 0，仍判通过（审计 M-04 反例）。既有文件在校验期间
        # 被改写或删除即判失败：这挡住的是"先改坏再退出 0"，**不解决测试是否充分**（M-04
        # 的本体是设计边界，见方案第六节）。新建文件（缓存、覆盖率报告）不算改写。
        trace({"role": role, "profile": f"verify:{profile_name}", "argv": argv, "cwd": str(cwd), "executed": True,
               "exit_code": proc.returncode, "failure_code": "VERIFY_FAILED", "tampered": tampered[:8]})
        return {"passed": False, "failure_code": "VERIFY_FAILED", "started_at": started, "ended_at": _now_iso(),
                "exit_code": proc.returncode, "argv_sha256": _sha256_str(json.dumps(argv)),
                "detail": f"校验期间既有文件被改写或删除，退出码不作数：{', '.join(tampered[:8])}"}
    trace({"role": role, "profile": f"verify:{profile_name}", "argv": argv, "cwd": str(cwd), "executed": True,
           "exit_code": proc.returncode, "duration_s": round(time.time() - t0, 1),
           "stdout_tail": (proc.stdout or "")[-600:], "stderr_tail": (proc.stderr or "")[-600:]})
    return {"passed": proc.returncode == 0, "failure_code": "" if proc.returncode == 0 else "VERIFY_FAILED",
            "started_at": started, "ended_at": _now_iso(), "exit_code": proc.returncode,
            "argv_sha256": _sha256_str(json.dumps(argv)),
            "detail": ((proc.stderr or "") + (proc.stdout or ""))[-400:]}


def cmd_trace(log: Path, n: int, full: bool) -> int:
    """人读执行轨迹：一行一次真实调用，答"这条命令到底跑没跑、跑的是什么"。"""
    if not log.is_file():
        print(f"暂无轨迹：{log}（尚未发生过外置调度）")
        return EXIT_OK
    rows = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for r in rows[-n:]:
        head = "✅" if r.get("exit_code") == 0 and r.get("failure_code") in (None, "") else "❌"
        argv = r.get("argv") or []
        shown = " ".join(argv[:4]) + (f" …(+{len(argv) - 4} 参数)" if len(argv) > 4 else "")
        print(f"{head} {r['ts']} [{r.get('role') or '-'}] {r.get('profile')} "
              f"exit={r.get('exit_code')} {r.get('duration_s', '-')}s {r.get('failure_code') or ''}")
        print(f"   $ {shown}")
        if full:
            print(f"   argv: {json.dumps(argv, ensure_ascii=False)}")
            for k in ("cwd", "deliverable", "deliverable_valid", "stdout_tail", "stderr_tail", "detail"):
                if r.get(k) not in (None, ""):
                    print(f"   {k}: {r[k]}")
    print(f"—— 共 {len(rows)} 条，显示最近 {min(n, len(rows))} 条 · {log}")
    return EXIT_OK


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
            row = {"id": a.get("assignment_id"), "role": a.get("role"), "status": a.get("status"),
                   "gate": "hard" if a.get("role") in HARD_ROLES else "soft", "mode": f"{mode}（{why}）",
                   "profile": a.get("command_profile")}
            # 依据指纹变了 ⇒ 既有 succeeded 失效：上一棒被改写，这一棒的产出不再对得上
            if a.get("target_path"):
                tp = (archive.parent / a["target_path"]).resolve()
                row["target"] = a["target_path"]
                row["stale"] = (not tp.exists()) or _sha256_file(tp) != a.get("target_sha256")
            if a.get("verify_profile"):
                row["verify_profile"] = a["verify_profile"]
            out["assignments"].append(row)
    else:
        # 区分"没有档案"与"档案在、只是还没指派"：直跑能往后者写，前者只出回执
        arch_tier, arch = find_archive_any(cwd)
        out["archivable"] = str(arch) if arch else None
        out["note"] = ("无指派记录；直跑 `run --role <角色> --out <交付物>` 会写入上述档案（第 %d 级）" % arch_tier
                       if arch else "无第 1/2 级档案；直跑只出回执不建档，第 3 级 roles.yaml 生效")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return EXIT_OK


# ---------------------------------------------------------------- 配置迁移（§3.4/§3.5）

def _tokenize_candidate(candidate: str) -> tuple[list[str], bool]:
    """保守分词：含引号/转义/不可判定字符 → (…)拒迁（人工仲裁 R3-01 B 收缩）。"""
    if any(ch in candidate for ch in "\"'\\") or not candidate.strip():
        return [], False
    return candidate.split(), True


def migrate_config(config_path: Path = LEGACY_WS_CONFIG, apply: bool = False) -> int:
    """旧布局（workspace-config.md 角色表）幂等字段级迁移：仅缺失写入不覆盖；
    角色表自由命令 → 结构化 profiles（保守判定）。新布局直接维护 roles.yaml，不经本函数。"""
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
    p.add_argument("--config", type=Path, default=LEGACY_WS_CONFIG, help="--migrate-config 的旧布局 workspace-config.md 路径")
    sub = p.add_subparsers(dest="cmd")
    pr = sub.add_parser("resolve", help="解析三级链，打印生效指派")
    pr.add_argument("--cwd", type=Path, default=Path.cwd())
    pu = sub.add_parser("run", help="执行指派（矩阵裁决 + CAS 写回）")
    pu.add_argument("--cwd", type=Path, default=Path.cwd())
    pu.add_argument("--assignment", help="档案内既有指派 ID；与 --role 二选一")
    pu.add_argument("--role", choices=ALL_ROLES, help="直跑：按第 3 级解析该角色，指派记录由本工具落笔（需 --out 或 --verify）")
    pu.add_argument("--out", type=Path, help="直跑模式的交付物路径或目录（相对 --cwd）")
    pu.add_argument("--target", type=Path, help="输入依据（上一棒产出）；记录指纹，依据变更后 resolve 标 stale")
    pu.add_argument("--verify", help="校验 profile 名；给定后成功判据为该命令退出码 0（代码型角色）")
    pu.add_argument("--prompt", help="任务提示词；缺省读 stdin")
    pu.add_argument("--ack", help="问题级豁免引用（hard blocked 时记录；有效性由 check_audit_gate.py 校验）")
    pu.add_argument("--owner", default=os.environ.get("USER", "manager"))
    pt = sub.add_parser("trace", help="查看执行轨迹（命令原文、退出码、耗时）")
    pt.add_argument("-n", type=int, default=20, help="显示最近 N 条")
    pt.add_argument("--full", action="store_true", help="展开完整 argv 与输出尾部")
    pt.add_argument("--log", type=Path, default=TRACE_LOG)
    pv = sub.add_parser("validate", help="校验 manifest 档案")
    pv.add_argument("--manifest", type=Path, required=True, help="capsule.yaml 或 workers.yaml")
    args = p.parse_args(argv)

    if args.migrate_config:
        return migrate_config(args.config, apply=args.apply)
    if args.cmd == "resolve":
        return cmd_resolve(args.cwd)
    if args.cmd == "trace":
        return cmd_trace(args.log, args.n, args.full)
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
        if bool(args.assignment) == bool(args.role):
            print(json.dumps({"error": "--assignment 与 --role 二选一"}, ensure_ascii=False))
            return EXIT_USAGE
        if args.role:
            if not args.out and not args.verify:
                print(json.dumps({"error": "--role 直跑须给 --out 交付物路径，或给 --verify 校验判据"}, ensure_ascii=False))
                return EXIT_USAGE
            if args.verify and args.role not in VERIFY_ROLES:
                print(json.dumps({"error": f"--verify 只适用于代码型角色 {sorted(VERIFY_ROLES)}，实得 {args.role}",
                                  "rule": "判据是角色属性；硬门禁角色无交付物即 blocked，不得以校验退出码顶替"}, ensure_ascii=False))
                return EXIT_USAGE
            if not args.out and args.role in HARD_ROLES:
                print(json.dumps({"error": f"硬门禁角色 {args.role} 必须给 --out 交付物路径",
                                  "rule": "独立性以产出为证；无交付物的硬角色一律不得记 succeeded"}, ensure_ascii=False))
                return EXIT_USAGE
            return run_direct(args.cwd, args.role, prompt, args.out, args.owner, args.target, args.verify)
        return run_assignment(args.cwd, args.assignment, prompt, args.ack, args.owner)
    p.print_help()
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
