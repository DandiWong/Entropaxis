#!/usr/bin/env python3
"""工作区健康度与上下文瘦身体检工具 (Workspace & Context Linter).

用于定期自动维护系统规则与工作区健康度，确保 Agent 在任何情况下保持上下文窗口极简、高信噪比。
执行方式:
  python3 .entropaxis/tools/lint_workspace.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    from . import paths
    from . import stamp_data_provenance as provenance
except ImportError:
    import paths
    import stamp_data_provenance as provenance

HERE = Path(__file__).resolve().parent
ROOT = paths.WORKSPACE_ROOT

# 工作区常驻层预算
RESIDENT_MAX_LINES = 50
PROJECT_AGENTS_MAX_LINES = 60
MAX_CURRENT_STATE_LINES = 120

# 按需层预算：单个规则文件行数建议线。不设 rules/ 目录总量上限——规则数量随业务自然增长，
# 总量封顶会逼迫把不相关内容塞进同一文件，反而破坏正交收敛。
RULE_MAX_LINES = 120
# 跨规则文件连续文本重复检测（字符级窗口比对，不理解语义）：归一化后连续重复字符数达该窗口即判定为复述（应改为引用）。
# 25 字约合一个完整分句：实测该阈值召回全部真实复述且零误报；再收紧会把中英双写等
# 非复述形态一并命中（那属于《表达文风》L1 的处理范围，不应在此告警）。
RULE_DUP_WINDOW = 25
RULE_DUP_MAX_REPORTS = 8

EXCLUDE_PATTERNS = (paths.SYSTEM_DIRNAME, "Archive", "repoes", "skills", "node_modules", "graphify-out")

# 零系统绑定/零真实实体禁词按实例声明外置于 .entropaxis/data/rules/零系统绑定词表.md：
# 词表写死在这里，等于让"防止硬编码组织名"的检查本身成为控制面里唯一硬编码组织名的
# 文件，随版本库分发给每一个收件方（软件工程.md「检查按影响选」通用性维度「已知盲区」已记载该悖论）。
BINDING_WORDLIST = f"{paths.SYSTEM_DIRNAME}/data/rules/零系统绑定词表.md"
# 控制面禁止硬编码本地调试端点：外部系统交互必须经声明外置的看板/服务 CLI
FORBIDDEN_HOST_PATTERN = re.compile(r"127\.0\.0\.1|localhost")

# .entropaxis 健康度：控制面可发现、可渲染、可执行的最小契约。
SYSTEM_REQUIRED_DIRECTORIES = ("entrypoints", "rules", "schemas", "templates", "tools", "skills", "tests")
SYSTEM_REQUIRED_FILES = (
    "AGENTS.md",
    "README.md",
    "entrypoints/AGENTS.md",
    "entrypoints/CLAUDE.md",
    "tools/bootstrap.py",
    "tools/init_project.py",
    "tools/init_app.py",
    "tools/lint_workspace.py",
    "tools/open_file.py",
)
SYSTEM_TEMPLATE_VARIABLES = {
    "项目名",
    "开始日期",
    "用途和目标",
    "相关人员",
    "下一关键事件",
    "已有材料",
    "项目独有知识",
    "共享知识",
    "敏感级别",
    "看板项目ID",
    "ROOT_AGENTS_PATH",
    "应用名",
    "产品定位与核心价值",
    "用户与使用场景",
}

def check_resident_budget(root: Path) -> list[str]:
    """检查常驻层文件行数与体积预算。"""
    issues = []
    root_agents = root / "AGENTS.md"
    if root_agents.exists():
        lines = root_agents.read_text(encoding="utf-8").splitlines()
        if len(lines) > RESIDENT_MAX_LINES:
            issues.append(
                f"[薄常驻超标] 根目录 AGENTS.md 共 {len(lines)} 行，超过预算上限 {RESIDENT_MAX_LINES} 行！请将具体业务/交付物细则下沉至 rules/。"
            )

    # 检查各第一方项目 AGENTS.md
    for p in root.glob("**/AGENTS.md"):
        if p == root_agents or not _is_first_party(p, root):
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
            cnt = len(lines)
            if cnt > PROJECT_AGENTS_MAX_LINES:
                issues.append(
                    f"[项目常驻偏重] {p.relative_to(root)} 共 {cnt} 行，超过建议上限 {PROJECT_AGENTS_MAX_LINES} 行。"
                )
            if any(line.strip() == "@AGENTS.md" for line in lines):
                issues.append(
                    f"[入口自引用] {p.relative_to(root)} 使用 @AGENTS.md 引用自身；应改为工作区路由或项目约束。"
                )
        except Exception:
            pass
    return issues

def check_current_state_bloat(root: Path) -> list[str]:
    """检查各项目 DECISIONS.md 或 _契约/当前状态.md 是否堆积历史。"""
    issues = []
    for pattern in ("**/DECISIONS.md", "**/_契约/当前状态.md"):
        for cs in root.glob(pattern):
            if not _is_first_party(cs, root):
                continue
            try:
                lines = cs.read_text(encoding="utf-8").splitlines()
                if len(lines) > MAX_CURRENT_STATE_LINES:
                    issues.append(
                        f"[决策底册堆积] {cs.relative_to(root)} 共 {len(lines)} 行（建议 $\\le {MAX_CURRENT_STATE_LINES}$ 行）。请检查是否有未归档历史结论，新结论应直接覆盖旧结论。"
                    )
            except Exception:
                pass
    return issues


def check_rule_deduplication(root: Path) -> list[str]:
    """检查 rules/ 各文件与 AGENTS.md 之间是否存在违背单一真源的冗余复制。"""
    issues = []
    rules_dir = root / paths.SYSTEM_DIRNAME / "rules"
    if not rules_dir.exists():
        return issues

    # 提取所有 rules 文件的二级标题与关键短语
    seen_headers: dict[str, list[str]] = {}
    for rf in rules_dir.glob("*.md"):
        try:
            content = rf.read_text(encoding="utf-8")
            for h in re.findall(r"^##\s+(.+)$", content, re.MULTILINE):
                h_clean = h.strip()
                if h_clean not in ("引言", "概述", "背景"):
                    seen_headers.setdefault(h_clean, []).append(rf.name)
        except Exception:
            pass

    for header, files in seen_headers.items():
        if len(files) > 1:
            issues.append(
                f"[潜在规则重复] 二级标题「{header}」同时出现在多个规则文件中: {', '.join(files)}。请确认是否违背单一真源原则。"
            )
    return issues


def check_rule_budget(root: Path) -> list[str]:
    """检查单个规则文件行数是否超出按需层预算。

    常驻层（AGENTS.md）已有 check_resident_budget 把关，但被按需读取的 rules/ 本身
    此前不受任何预算约束——这正是规则体量失控的结构性缺口。本检查补上该门禁。
    """
    issues = []
    rules_dir = root / paths.SYSTEM_DIRNAME / "rules"
    if not rules_dir.exists():
        return issues
    for rf in sorted(rules_dir.glob("*.md")):
        try:
            cnt = len(rf.read_text(encoding="utf-8").splitlines())
        except Exception:
            continue
        if cnt > RULE_MAX_LINES:
            issues.append(
                f"[规则超预算] rules/{rf.name} 共 {cnt} 行，超建议线 {RULE_MAX_LINES} 行（超 {cnt - RULE_MAX_LINES} 行）。"
                "按《表达文风》「规则瘦身」处理：判据留规则、操作步骤下沉 Skill、复述改引用。"
            )
    return issues


# 场景级联预算：一次动作实际读的不是一个规则文件，而是「入口规则 + 它直接指向的下游规则」。
# 取 5 倍单文件预算为线——一次动作不该需要读满 5 个预算规模的规则才知道怎么做。
SCENARIO_MAX_LINES = RULE_MAX_LINES * 5


def check_scenario_cascade_budget(root: Path) -> list[str]:
    """核验根入口每个路由场景的深度 1 级联读取量。

    check_rule_budget 逐文件把关，通过不代表场景通过：单个文件都达标，靠互相引用拼出的
    一次实际加载量仍可数倍于预算，而此前无人度量。
    只测深度 1：规则图近乎强连通（实测 18 个路由入口里 13 个的无界传递闭包是同一批 23 个
    文件），再往深只会得出「全库」这个无信息量的结论；深度 1 才对应 Agent 真正会读的范围。
    级联只计 rules/ 与 schemas/：data/ 实例声明的体量随各工作区业务增长，计入会让控制面
    的设计预算随实例数据浮动，规则作者也无从处置。
    """
    issues = []
    system = root / paths.SYSTEM_DIRNAME
    rules_dir = system / "rules"
    schemas_dir = system / "schemas"
    if not rules_dir.is_dir():
        return issues

    def _line_count(path: Path) -> int:
        try:
            return len(path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            return 0

    entries: set[Path] = set()
    outgoing: dict[Path, set[Path]] = {}
    for document, _target, resolved in _iter_local_links(root):
        resolved = Path(os.path.normpath(str(resolved)))
        if not resolved.is_file():
            continue
        if document.parent == system / "entrypoints" and resolved.parent == rules_dir:
            entries.add(resolved)
        elif document.parent == rules_dir and resolved.parent in (rules_dir, schemas_dir):
            outgoing.setdefault(document, set()).add(resolved)

    for entry in sorted(entries):
        cascade = {entry} | outgoing.get(entry, set())
        total = sum(_line_count(p) for p in cascade)
        if total > SCENARIO_MAX_LINES:
            downstream = sorted(p.name for p in cascade if p != entry)
            issues.append(
                f"[场景级联超预算] 路由入口 rules/{entry.name} 的深度 1 级联共 {total} 行"
                f"（{len(cascade)} 个文件），超建议线 {SCENARIO_MAX_LINES} 行。"
                f"下游：{'、'.join(downstream)}。"
                "按《表达文风》「规则瘦身」处理：把被多处引用的公共判据上收为单一真源，"
                "或将只服务于某一分支的引用下沉到该分支的 Skill。"
            )
    return issues


MAX_RULE_SYNTAX_TAX_PCT = 10.0
MAX_ENTRYPOINT_SYNTAX_TAX_PCT = 5.0
def _compute_syntax_tax_ratio(content: str) -> float:
    total_chars = len(content)
    if total_chars == 0:
        return 0.0
    lines = content.splitlines()
    table_sep_pattern = re.compile(r"^\|?[\s\-:|]+\|?$")
    tax_chars = 0
    for line in lines:
        stripped = line.strip()
        if table_sep_pattern.match(stripped) or (stripped.startswith("|") and stripped.endswith("|")):
            tax_chars += len(line)
        excess_spaces = re.findall(r"(?<=\S) {2,}(?=\S)", line)
        tax_chars += sum(len(m) for m in excess_spaces)
        tax_chars += len(re.findall(r"[┌┬┐├┼┤└┴┘│─═║╔╦╗╠╬╣╚╩╝]", line))
    return round((tax_chars / total_chars * 100), 2)


def check_syntax_tax_budget(root: Path) -> list[str]:
    """机械核验规则库与根入口的语法税预算（防止宽大对齐表格与冗余空格腐蚀上下文）。"""
    issues = []
    system = root / paths.SYSTEM_DIRNAME
    entry_ag = system / "entrypoints" / "AGENTS.md"
    if entry_ag.is_file():
        ratio = _compute_syntax_tax_ratio(entry_ag.read_text(encoding="utf-8"))
        if ratio > MAX_ENTRYPOINT_SYNTAX_TAX_PCT:
            issues.append(f"[常驻入口语法税超标] entrypoints/AGENTS.md 语法税 {ratio}% > {MAX_ENTRYPOINT_SYNTAX_TAX_PCT}%；须去表化为紧凑列表。")
    rules_dir = system / "rules"
    if rules_dir.is_dir():
        for rf in sorted(rules_dir.glob("*.md")):
            ratio = _compute_syntax_tax_ratio(rf.read_text(encoding="utf-8"))
            if ratio > MAX_RULE_SYNTAX_TAX_PCT:
                issues.append(f"[规则语法税超标] rules/{rf.name} 语法税 {ratio}% > {MAX_RULE_SYNTAX_TAX_PCT}%；改用冒号键值行或紧凑列表消税。")
    return issues


def _normalize_rule_text(content: str) -> str:
    """归一化规则正文，供跨文件连续文本重复检测使用。

    剔除代码块与行内代码：命令原文允许跨文件重复（漂移代价高于 Token 收益）；
    剔除强调标记、标题井号与空白：不承载判据，否则同义片段会因排版差异漏检。
    """
    content = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
    content = re.sub(r"`[^`]*`", "", content)
    content = re.sub(r"\]\([^)]*\)", "]", content)  # 链接目标是指针不是复述，剔除后只留链接文字
    kept = []
    for line in content.splitlines():
        s = line.strip()
        if not s or set(s) <= set("|-: "):  # 空行与表格分隔行
            continue
        kept.append(s)
    return re.sub(r"[\s*_>#]", "", "".join(kept))


def check_rule_text_repetition(root: Path) -> list[str]:
    """检测跨规则文件的连续文本重复片段，补足字面标题去重的盲区。

    check_rule_deduplication 只比对 `##` 标题字符串是否相同。本检查以滑动窗口找出在
    2 个以上文件中同时出现的连续字符片段，命中即应改为「见 X.md §N」引用；这是字符级
    重复检测，不理解语义，检测不到的表述不同但含义冲突的规则不在本检查覆盖范围内。
    """
    issues = []
    rules_dir = root / paths.SYSTEM_DIRNAME / "rules"
    if not rules_dir.exists():
        return issues

    norm: dict[str, str] = {}
    for rf in sorted(rules_dir.glob("*.md")):
        try:
            norm[rf.name] = _normalize_rule_text(rf.read_text(encoding="utf-8"))
        except Exception:
            continue

    window_owners: dict[str, set[str]] = {}
    for name, text in norm.items():
        for i in range(len(text) - RULE_DUP_WINDOW + 1):
            window_owners.setdefault(text[i : i + RULE_DUP_WINDOW], set()).add(name)

    # 把同一文件内相邻的命中窗口合并为极大片段，避免同一处重复被拆成数十条告警
    fragments: dict[str, set[str]] = {}
    for name, text in norm.items():
        run_start = None
        for i in range(len(text) - RULE_DUP_WINDOW + 1):
            owners = window_owners.get(text[i : i + RULE_DUP_WINDOW], set())
            hit = len(owners) > 1
            if hit and run_start is None:
                run_start = i
            elif not hit and run_start is not None:
                frag = text[run_start : i - 1 + RULE_DUP_WINDOW]
                fragments.setdefault(frag, set()).update(
                    window_owners.get(text[run_start : run_start + RULE_DUP_WINDOW], set())
                )
                run_start = None
        if run_start is not None:
            frag = text[run_start:]
            fragments.setdefault(frag, set()).update(
                window_owners.get(text[run_start : run_start + RULE_DUP_WINDOW], set())
            )

    # 同一处复述会在参与的每个文件各产出一条等长片段，按 owners 归并后只保留最长的一条
    by_owners: dict[tuple[str, ...], str] = {}
    for frag, owners in fragments.items():
        key = tuple(sorted(owners))
        if len(frag) > len(by_owners.get(key, "")):
            by_owners[key] = frag

    ranked = sorted(by_owners.items(), key=lambda kv: len(kv[1]), reverse=True)
    for owners, frag in ranked[:RULE_DUP_MAX_REPORTS]:
        preview = frag[:50] + ("…" if len(frag) > 50 else "")
        issues.append(
            f"[跨文件复述] {len(frag)} 字片段同时出现在 {', '.join(owners)}：「{preview}」。"
            "请择一保留正文，其余改为「见 X.md §N」+ 一句用途说明。"
        )
    if len(ranked) > RULE_DUP_MAX_REPORTS:
        issues.append(f"[跨文件复述] 另有 {len(ranked) - RULE_DUP_MAX_REPORTS} 处较短复述未列出。")
    return issues


def check_schema_conformance(root: Path) -> list[str]:
    """按 .entropaxis/schemas/ 校验结构化契约。

    此前 Front Matter 的取值域、审计报告的问题标注格式，
    契约都只存在于各自解析器的正则里——改规则的人无从得知自己在破坏一个解析器。
    """
    tools = root / paths.SYSTEM_DIRNAME / "tools"
    if not (tools / "validate_schema.py").exists():
        return []
    sys.path.insert(0, str(tools))
    try:
        import validate_schema as vs
    except Exception as exc:  # noqa: BLE001 - 工具不可用不应阻断整体体检
        return [f"[schema 校验不可用] {exc}"]
    finally:
        if str(tools) in sys.path:
            sys.path.remove(str(tools))
    return [f"[违反 schema] {e}" for e in vs.check_audit_report_schema_selftest()]


def check_data_source_mapping(root: Path) -> list[str]:
    """双向核验 data/ 路径与 .entropaxis/ 定义方的对应关系。

    `data/` 按定义方分两个桶，路径本身即指向来源（Skill 配置随 Skill 目录，不在 data/）：
      data/templates/X  ⟺ .entropaxis/templates/instance/X.template.*
      data/rules/*      ⟺ 由某条规则声明（具体哪条见文件头 source 字段）
    双向检查能同时抓出孤儿实例文件与失配模板，防结构随时间漂移。
    """
    issues = []
    sys_dir = root / paths.SYSTEM_DIRNAME
    data_dir = sys_dir / "data"
    if not data_dir.is_dir():
        return issues

    tpl_dir = data_dir / "templates"
    if tpl_dir.is_dir():
        for p in sorted(tpl_dir.glob("*")):
            if not p.is_file() or p.suffix not in provenance.DATA_SUFFIXES:
                continue
            expect = sys_dir / "templates" / "instance" / f"{p.stem}.template{p.suffix}"
            if not expect.exists():
                issues.append(
                    f"[实例孤儿] .entropaxis/data/templates/{p.name} 找不到对应模板 {expect.relative_to(root)}；"
                    "它不是模板渲染产物，应移入 .entropaxis/data/rules/（规则声明）或所属 Skill 自身目录。"
                )

    # 反向：模板存在却无实例。拆分 templates/instance 与 templates/project 后
    # 该目录内每个模板都必然对应一个 data/templates/ 实例，可严格双向核验。
    sys_tpl = sys_dir / "templates" / "instance"
    if sys_tpl.is_dir() and tpl_dir.is_dir():
        for t in sorted(sys_tpl.glob("*.template.*")):
            stem, suffix = t.name.split(".template", 1)
            if not (tpl_dir / f"{stem}{suffix}").exists():
                issues.append(
                    f"[实例未渲染] .entropaxis/templates/instance/{t.name} 没有对应实例 "
                    f".entropaxis/data/templates/{stem}{suffix}；运行 `python3 .entropaxis/tools/bootstrap.py` 渲染。"
                )

    # Skill 自包含：配置随 Skill 目录、凭据放用户级目录，控制面 data/ 不承载任何 Skill 配置
    skl_dir = data_dir / "skills"
    if skl_dir.is_dir() and any(skl_dir.iterdir()):
        issues.append(
            "[Skill 外部配置] .entropaxis/data/skills/ 下仍有内容；Skill 须自包含，"
            "把配置移回 .entropaxis/skills/<名>/（私有 Skill 由其 .gitignore 排除），凭据放用户级目录或环境变量。"
        )

    for p in sorted(data_dir.glob("*")):
        if p.is_file() and p.suffix in (".md", ".json"):
            issues.append(
                f"[未归桶] .entropaxis/data/{p.name} 位于顶层。按来源归入 templates/（模板渲染）、"
                "rules/（规则声明）或 skills/<名>/（Skill 私有）。"
            )
    return issues


def check_data_provenance(root: Path) -> list[str]:
    """检查 data/ 顶层实例文件是否声明来源与写入策略。

    `data/` 是人工真源与实例配置所在地，缺少来源标记时人眼无法判断某文件从哪来、
    谁在维护、能否重写——这正是人工仲裁配置被 Agent 整体覆盖的前置条件。
    只查顶层 .md/.json；`credentials/` 及子目录含凭据，不读不列举。
    """
    issues = []
    data_dir = root / paths.SYSTEM_DIRNAME / "data"
    if not data_dir.is_dir():
        return issues
    # 只扫两个来源桶；credentials/ 与 docs/ 不读不列举（前者含凭据，后者是研究产物）
    for p in sorted(q for b in provenance.SOURCE_BUCKETS for q in (data_dir / b).rglob("*")):
        if not p.is_file() or p.suffix not in provenance.DATA_SUFFIXES:
            continue
        if not provenance.has_provenance(p):
            issues.append(
                f"[实例文件缺来源标记] .entropaxis/data/{p.relative_to(data_dir)} 未声明 source/managed_by/policy。"
                "运行 `python3 .entropaxis/tools/stamp_data_provenance.py` 补盖。"
            )
    return issues


def _is_nested_git_repo_path(path: Path, root: Path) -> bool:
    """path 是否位于工作区根之外、自带独立 .git 的上游/第三方目录内（含 .entropaxis/ 自身）。
    与 registry.md「自带 .git 且非工作区成员的上游仓库不纳入注册表」的排除口径一致，
    防止把有独立代码/内容契约的嵌套仓库误判为待规范化的工作区项目薄壳。"""
    current = path.parent
    while True:
        if (current / ".git").exists():
            return True
        if current == root or current.parent == current:
            return False
        current = current.parent


def _is_first_party(path: Path, root: Path) -> bool:
    """path 是否属于本工作区第一方内容（体检只对第一方内容求值）。

    排除两类：命名约定上的非项目目录（EXCLUDE_PATTERNS），以及自带 `.git` 的上游/
    第三方嵌套仓库——后者有自己的内容契约，《01_根系统治理》归属表已声明「第三方或
    上游包约束归其自带的 AGENTS.md」，不得按本工作区规范去改写。
    """
    return not any(ex in str(path) for ex in EXCLUDE_PATTERNS) and not _is_nested_git_repo_path(path, root)


def check_claude_md_thin_shell(root: Path) -> list[str]:
    """所有 CLAUDE.md 必须是纯薄壳：一行标题 + 唯一一行 @AGENTS.md，无其他 import 或正文。
    例外：若 CLAUDE.md 是指向同目录 AGENTS.md 的符号链接（open-slide 等框架约定），视为等价薄壳；
    嵌套独立 git 仓库（见 `_is_nested_git_repo_path`）视为上游内容，不纳入检查。"""
    issues = []
    for cm in root.glob("**/CLAUDE.md"):
        cm_str = str(cm)
        if any(ex in cm_str for ex in ("Archive", "repoes", "node_modules")):
            continue
        if _is_nested_git_repo_path(cm, root):
            continue
        try:
            # 框架约定例外：CLAUDE.md → AGENTS.md 符号链接 = 等价薄壳
            if cm.is_symlink():
                target = os.readlink(str(cm))
                if target in ("AGENTS.md", "./AGENTS.md"):
                    continue  # ponytail: 等价薄壳，不强制改格式
            text = cm.read_text(encoding="utf-8")
            lines = [l for l in text.splitlines() if l.strip()]
            if len(lines) != 2 or not lines[0].startswith("#") or lines[1].strip() != "@AGENTS.md":
                issues.append(
                    f"[薄壳违规] {cm.relative_to(root)} 不是纯薄壳（应为：一行标题 + @AGENTS.md，无其他内容）。"
                )
        except Exception:
            pass
    return issues


def fix_claude_md_thin_shell(root: Path) -> list[str]:
    """将 check_claude_md_thin_shell 判定为违规的项目 CLAUDE.md 原地改写为标准薄壳
    （一行标题 + @AGENTS.md）。排除名单、符号链接例外与嵌套独立 git 仓库排除均与检测
    函数保持一致（单一真源见 `_is_nested_git_repo_path`），只改写真正判定为违规的文件；
    已合规文件不动。"""
    fixed = []
    for cm in root.glob("**/CLAUDE.md"):
        cm_str = str(cm)
        if any(ex in cm_str for ex in ("Archive", "repoes", "node_modules")):
            continue
        if _is_nested_git_repo_path(cm, root):
            continue
        try:
            if cm.is_symlink():
                target = os.readlink(str(cm))
                if target in ("AGENTS.md", "./AGENTS.md"):
                    continue
            text = cm.read_text(encoding="utf-8")
            lines = [l for l in text.splitlines() if l.strip()]
            if len(lines) == 2 and lines[0].startswith("#") and lines[1].strip() == "@AGENTS.md":
                continue
            title = (
                lines[0].strip()
                if lines and lines[0].strip().startswith("#")
                else f"# {cm.parent.name} · Claude Code 入口"
            )
            cm.write_text(f"{title}\n@AGENTS.md\n", encoding="utf-8")
            # 全仓统一用 posix 分隔符：relative_to 在 Windows 上默认给反斜杠，
            # 与规则文本、测试断言和其余体检输出的路径写法不一致。
            fixed.append(cm.relative_to(root).as_posix())
        except Exception:
            pass
    return fixed


def _tracked_files(system: Path) -> set[Path] | None:
    """返回版本库跟踪文件集合；非 git 环境返回 None（退化为全文件扫描）。
    业务私有 Skill 等由 .gitignore 声明豁免，不参与零绑定检查。
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(system), "ls-files"], capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            return None
        return {system / line.strip() for line in r.stdout.splitlines() if line.strip()}
    except Exception:
        return None


def _forbidden_bindings(root: Path) -> list[str]:
    """读取实例声明的禁用实体词表（每行一个 `- 词`），小写归一。

    文件缺失或为空时返回空表：这是软降级——结构性检查（本地端点）继续生效，实体词检查
    停用，由调用方留痕告警，不静默假装通过（《系统演进准则》第 7 节「同受约束」）。
    此处**不得**保留任何硬编码兜底词表：那等于把组织实体名重新写回控制面并随版本库分发，
    正是本函数外置化要消除的东西（同准则第 3 条「检查器同受约束」）。
    """
    wordlist = root / BINDING_WORDLIST
    try:
        text = wordlist.read_text(encoding="utf-8")
    except OSError:
        return []
    terms = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- ") and not line.startswith("- ["):
            term = line[2:].split("（", 1)[0].split("#", 1)[0].strip().strip("`").lower()
            if term:
                terms.append(term)
    return terms


def check_rules_zero_system_binding(root: Path) -> list[str]:
    """控制面（rules/、entrypoints/、templates/、tools/、tests/、skills/*/SKILL.md）
    不得出现具体业务系统绑定或真实实体；tools/skills/tests 可执行内容额外禁止硬编码本地端点。
    仅检查版本库跟踪文件（非 git 环境退化全扫），业务私有 Skill 依 .gitignore 豁免。"""
    issues = []
    system = root / paths.SYSTEM_DIRNAME
    tracked = _tracked_files(system)
    forbidden = _forbidden_bindings(root)
    if not forbidden:
        # 降级必须留痕：静默跳过会让"检查通过"与"检查没跑"在输出上无法区分
        issues.append(
            f"[实体词表未声明] 未读到 {BINDING_WORDLIST}，本轮仅执行结构性端点检查，"
            "真实实体词检查已降级停用；新工作区请按《系统演进准则》零系统绑定铁律补齐词表。"
        )

    def _glob(base: Path, pattern: str) -> list[Path]:
        if not base.exists():
            return []
        files = [p for p in base.glob(pattern) if p.is_file()]
        if tracked is not None:
            files = [p for p in files if p in tracked]
        return files

    binding_targets: list[Path] = []
    binding_targets += _glob(system / "rules", "*.md")
    binding_targets += _glob(system / "entrypoints", "*.md")
    binding_targets += _glob(system / "templates", "**/*")
    # 词表外置后 lint 自身不再持有任何禁词，此前的自我豁免随之取消：
    # 检查器与被检查者同一把尺子，控制面里再出现实体词就该被自己抓出来。
    operational_targets: list[Path] = [
        p for p in _glob(system / "tools", "*.py") if p.name != "lint_workspace.py"
    ]
    operational_targets += _glob(system / "skills", "*/SKILL.md")
    operational_targets += _glob(system / "tests", "*.py")
    binding_targets += operational_targets

    for rf in binding_targets:
        try:
            content = rf.read_text(encoding="utf-8").lower()
        except Exception:
            continue
        for keyword in forbidden:
            if keyword in content:
                issues.append(
                    f"[规则系统绑定] {rf.relative_to(root)} 含禁用关键词「{keyword}」；应为零系统绑定。"
                )
    for rf in operational_targets:
        try:
            content = rf.read_text(encoding="utf-8")
        except Exception:
            continue
        # rules/ 允许在说明文本里举例引用本地端点写法，仅对可执行内容做硬拦截
        if FORBIDDEN_HOST_PATTERN.search(content):
            issues.append(
                f"[硬编码本地端点] {rf.relative_to(root)} 出现 127.0.0.1/localhost；"
                "外部系统交互必须经声明外置的看板/服务 CLI，不得硬编码本地调试地址。"
            )
    return issues


def check_system_layout(root: Path) -> list[str]:
    """检查 .entropaxis 控制面所需目录和入口文件。"""
    system = root / paths.SYSTEM_DIRNAME
    issues = []
    for directory in SYSTEM_REQUIRED_DIRECTORIES:
        if not (system / directory).is_dir():
            issues.append(f"[系统结构缺失] .entropaxis/{directory}/ 不存在。")
    for filename in SYSTEM_REQUIRED_FILES:
        if not (system / filename).is_file():
            issues.append(f"[系统入口缺失] .entropaxis/{filename} 不存在。")
    return issues


def check_system_entry_sync(root: Path) -> list[str]:
    """检查根入口是否与 .entropaxis/entrypoints 的唯一真源一致。"""
    system = root / paths.SYSTEM_DIRNAME
    issues = []
    for filename in ("AGENTS.md", "CLAUDE.md"):
        source = system / "entrypoints" / filename
        target = root / filename
        if not source.is_file() or not target.is_file():
            continue
        if target.read_text(encoding="utf-8") != source.read_text(encoding="utf-8"):
            issues.append(
                f"[根入口漂移] {filename} 与 .entropaxis/entrypoints/{filename} 内容不一致；运行 bootstrap.py 恢复。"
            )
    return issues


def _iter_local_links(root: Path):
    """遍历 .entropaxis 内 Markdown 的显式本地链接，产出 (文档, 原始 target, 解析后路径)。"""
    system = root / paths.SYSTEM_DIRNAME
    link_pattern = re.compile(r"\[[^\]]*]\(([^)\s]+)(?:\s+[^)]*)?\)")
    tracked = _tracked_files(system)
    for document in system.rglob("*.md"):
        # Skill 包内随附的冻结参考文档（skills/<name>/references/**）不参与路由断链检查：
        # 其内部链接属于第三方/上游文档结构，不构成工作区路由契约。
        parts = document.relative_to(system).parts
        if len(parts) >= 3 and parts[0] == "skills" and parts[2] == "references":
            continue
        # 私有 Skill 依 .gitignore 整体排除出版本库（《技能设计》2.2），其内部引用不构成
        # 分发契约；只核验跟踪集，与第 13 项零系统绑定检查的作用域保持一致。
        if tracked is not None and document not in tracked:
            continue
        text = document.read_text(encoding="utf-8")
        # 同一文档内同一目标只报一次：`[`.entropaxis/data/x.md`](../data/x.md)` 这种"链接
        # 文字本身就是行内代码路径"的写法在规则正文里很常见，不去重会把一处引用报成两条。
        seen: set[str] = set()

        def _emit(target: str, resolved: Path):
            key = os.path.normpath(str(resolved))
            if key in seen:
                return None
            seen.add(key)
            return document, target, resolved

        for target in link_pattern.findall(text):
            target = target.strip("<>")
            if not target or target.startswith(("#", "/", "~", "http:", "https:", "mailto:")):
                continue
            path = target.split("#", 1)[0]
            if not path:
                continue
            base = root if document.parent == system / "entrypoints" else document.parent
            item = _emit(target, base / path)
            if item:
                yield item
        for target in _inline_code_paths(text):
            item = _emit(target, root / target)
            if item:
                yield item


# 反引号内联的工作区绝对路径（`.entropaxis/...`，含其下的 data/ 实例面）。规则正文引用
# 工具与实例声明时大量使用这种形态（如 `python3 .entropaxis/tools/init_capsule.py`），
# 而它不是 Markdown 链接——只扫 `](...)` 会让这类引用成为门禁盲区，悬空到分发后才被
# 收件方发现。data/ 现已嵌套在 .entropaxis/ 之内，前缀由此前的双分支塌缩为单一前缀。
# 扩展名尾部加 (?![\w*.]) 排除被截断的通配模板（`X.template.*`）。
_INLINE_PATH_PATTERN = re.compile(
    r"(?<![\w/.-])(\.entropaxis/[^\s`,，。、；：!?()（）\[\]\"'|]*\.[A-Za-z0-9]{1,8}(?![\w*.]))"
)


def _inline_code_paths(text: str):
    """产出 Markdown 行内代码中出现的、以 `.entropaxis/` 开头的工作区相对路径。"""
    for code in re.findall(r"`([^`\n]*?)`", text):
        for target in _INLINE_PATH_PATTERN.findall(code):
            # 含占位符的示例路径（`.entropaxis/tools/<name>.py`）不是真实引用，跳过。
            if any(ch in target for ch in "<>{}*"):
                continue
            yield target


def _points_into_data(path: Path, root: Path) -> bool:
    """判断链接目标是否落在 .entropaxis/data/ 实例面内（两侧同样 resolve，兼容 /tmp 软链前缀）。"""
    try:
        path.resolve().relative_to((root / paths.SYSTEM_DIRNAME / "data").resolve())
        return True
    except ValueError:
        return False


def check_system_markdown_links(root: Path) -> list[str]:
    """检查 .entropaxis 内 Markdown 显式本地链接不指向不存在的路径。

    指向 `.entropaxis/data/` 的实例声明引用交由第 22 项单独核验：`data/rules/` 桶按定义
    无模板（见 `控制面布局.md`「data/ 目录结构」），首次使用前必然不存在——把它算作
    阻断项，等于让分发出去的系统在新环境里开箱即红。
    """
    issues = []
    for document, target, resolved in _iter_local_links(root):
        if _points_into_data(resolved, root):
            continue
        if not resolved.exists():
            issues.append(
                f"[路由断链] {document.relative_to(root)} → {target} 不存在。"
            )
    return issues


def check_data_declaration_links(root: Path) -> list[str]:
    """核验规则正文引用的 `.entropaxis/data/` 实例声明在本工作区是否已落地。

    `01_根系统治理.md` 审计流程第 2 步要求盘点"新初始化/分发场景下悬空的 data/ 实例
    声明引用"。这类文件由规则在首次使用时创建，缺失是合法初始态而非契约破损，故只报
    建议不阻断；但必须报出来，否则系统分发到新环境后没人知道哪些声明还是空的。
    """
    issues = []
    for document, target, resolved in _iter_local_links(root):
        if not _points_into_data(resolved, root) or resolved.exists():
            continue
        issues.append(
            f"[实例声明待落地] {document.relative_to(root)} → {target} 尚未创建；"
            "该文件由引用它的规则在首次使用时生成，新工作区属正常初始态。"
        )
    return issues


def check_system_templates(root: Path) -> list[str]:
    """检查模板完备性与变量契约，保证两个脚手架可独立渲染。

    templates/ 按消费方分两个子目录：instance/ 渲染到 data/（bootstrap），
    project/ 是项目脚手架（init_project / init_app）。本检查只管后者。
    """
    templates = root / paths.SYSTEM_DIRNAME / "templates" / "project"
    issues = []
    required = (
        "AGENTS.template.md",
        "CLAUDE.template.md",
        "README.template.md",
        "DECISIONS.template.md",
        "Changelog.template.md",
        "ReleaseNote.template.md",
        "知识库索引.template.md",
        "DocsIndex.template.md",
        "Product.template.md",
        "Tasks.template.md",
    )
    for filename in required:
        template = templates / filename
        if not template.is_file():
            issues.append(f"[模板缺失] .entropaxis/templates/project/{filename} 不存在。")
            continue
        text = template.read_text(encoding="utf-8")
        variables = set(re.findall(r"{{([^{}]+)}}", text))
        unknown = variables - SYSTEM_TEMPLATE_VARIABLES
        if unknown:
            issues.append(
                f"[模板变量未知] .entropaxis/templates/{filename} 包含未声明变量：{', '.join(sorted(unknown))}。"
            )
        if "{{" in re.sub(r"{{[^{}]+}}", "", text) or "}}" in re.sub(r"{{[^{}]+}}", "", text):
            issues.append(f"[模板变量失配] .entropaxis/templates/{filename} 含未闭合变量标记。")
    return issues


def check_system_skills(root: Path) -> list[str]:
    """检查每个系统 Skill 的入口与元数据名称。"""
    skills = root / paths.SYSTEM_DIRNAME / "skills"
    issues = []
    if not skills.is_dir():
        return issues
    for skill_dir in skills.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            issues.append(f"[Skill入口缺失] {skill_dir.relative_to(root)}/SKILL.md 不存在。")
            continue
        text = skill_file.read_text(encoding="utf-8")
        name = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
        description = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
        if not name or name.group(1).strip().strip('"') != skill_dir.name:
            issues.append(
                f"[Skill名称失配] {skill_file.relative_to(root)} 的 name 必须是 {skill_dir.name}。"
            )
        if not description:
            issues.append(f"[Skill路由缺失] {skill_file.relative_to(root)} 缺少 description 触发描述。")
    return issues


# Skill 正文里对根系统的硬依赖形态：`.entropaxis/tools/x.py` 是跨 Skill 的控制面工具，
# 独立安装的机器上必然不存在，写成无条件前置即跑不起来。
# 只匹配 tools/：`.entropaxis/skills/<自身名>/…` 属自身路径写法，Skill 常以它作为
# "工作区内安装位置"的示例与全局安装形式并列，纳入会产生大量误报。
_SYSTEM_DEP_PATTERN = re.compile(r"\.entropaxis/tools/[\w\-/]+\.py")
_OPTIONAL_MARKERS = ("存在时", "不存在", "若工作区提供", "工作区提供", "仅在", "独立安装")


def check_skill_system_independence(root: Path) -> list[str]:
    """可分发 Skill 不得把 `.entropaxis/` 写成运行前置（《技能设计》2.2 独立运行铁律）。

    Skill 要能脱离本工作区独立安装运行，`.entropaxis/tools/x.py` 那条路径在收件方
    只装了一个 Skill 的机器上根本不存在。控制面 Skill（frontmatter 声明
    `scope: control-plane`）以 `.entropaxis/` 为作业对象，按定义豁免。
    """
    skills = root / paths.SYSTEM_DIRNAME / "skills"
    if not skills.is_dir():
        return []
    issues = []
    # 不按版本库跟踪集过滤：私有 Skill 同样要独立分发（6.3 分发包）与独立运行，
    # 「不随 git 走」不等于「不用能独立跑」。
    for skill_file in sorted(skills.glob("*/SKILL.md")):
        text = skill_file.read_text(encoding="utf-8")
        front = text.split("\n---", 2)[0] if text.startswith("---") else ""
        if "scope: control-plane" in front:
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if not _SYSTEM_DEP_PATTERN.search(line):
                continue
            # 紧邻上一行的注释是标注可选性的惯用位置，一并纳入判定
            context = line + (lines[index - 1] if index else "")
            if any(marker in context for marker in _OPTIONAL_MARKERS):
                continue
            issues.append(
                f"[Skill 根系统硬依赖] {skill_file.relative_to(root)} 行内 "
                f"`{line.strip()[:60]}` 把 .entropaxis/ 路径写成运行前置；"
                "独立安装时该路径不存在。改为相对自身目录定位，或标注为「存在时才调用」的可选增强；"
                "确属控制面 Skill 则在 frontmatter 声明 metadata.scope: control-plane。"
            )
    return issues


def check_system_tools_compile(root: Path) -> list[str]:
    """编译 .entropaxis/tools 下脚本，阻止控制面工具语法损坏。"""
    tools = root / paths.SYSTEM_DIRNAME / "tools"
    issues = []
    if not tools.is_dir():
        return issues
    for tool in tools.glob("*.py"):
        try:
            compile(tool.read_text(encoding="utf-8"), str(tool), "exec")
        except SyntaxError as error:
            issues.append(
                f"[工具语法错误] {tool.relative_to(root)}:{error.lineno}: {error.msg}"
            )
    return issues
def _registered_top_dirs(registry: Path) -> tuple[set[str], set[str]]:
    """解析注册表，返回 (项目映射表登记的顶层目录, 排除规则声明的顶层目录)。

    两者分开返回而非合并：**映射表是否为空**是"这个工作区有没有开始登记项目"的判定信号，
    合并进排除规则后就分不出来了——而排除规则在模板里天然非空（`repo/`、`Archive/` 等）。
    """
    table_tops: set[str] = set()
    exclude_tops: set[str] = set()
    if not registry.exists():
        return table_tops, exclude_tops
    table_part, _, exclude_part = registry.read_text(encoding="utf-8").partition("## 排除规则")
    for line in table_part.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("| 项目 ID") or line.startswith("|---"):
            continue
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if len(cols) >= 3:
            for d in re.findall(r"`([^`]+)`", cols[2]):
                d = d.strip()
                if d and "+" not in d:
                    table_tops.add(d.rstrip("/").split("/")[0])
    for token in re.findall(r"`([^`]+)`", exclude_part):
        token = token.strip().lstrip("*/").rstrip("/")
        if token:
            exclude_tops.add(token.split("/")[0])
    return table_tops, exclude_tops


def check_registry_population(root: Path) -> list[str]:
    """注册表还没登记任何项目时，把待登记的根目录报成建议项。

    收件方的工作区在接入本系统之前就已经有自己的目录结构（而且和任何既有工作区都不一样）。
    第 7 项的反向校验在那一刻必然全量命中——首次体检直接红屏，这不是缺陷而是初始态。
    此处以建议项把同一事实说清楚，登记任一项目后第 7 项自动接管为阻断校验。
    """
    registry = root / paths.SYSTEM_DIRNAME / "data" / "templates" / "registry.md"
    if not registry.exists():
        return []
    table_tops, exclude_tops = _registered_top_dirs(registry)
    if table_tops:
        return []
    pending = [
        c.name for c in sorted(root.iterdir())
        if c.is_dir() and not c.name.startswith(".") and c.name not in exclude_tops
    ]
    if not pending:
        return []
    return [
        f"[注册表待登记] 项目映射表尚无任何项目，{len(pending)} 个根目录待归属："
        f"{'、'.join(pending)}。用 `python3 .entropaxis/tools/init_project.py <项目名>` 立项，"
        "或把非项目目录写进注册表「排除规则」；登记任一项目后第 7 项转为阻断校验。"
    ]


def check_declared_writers(root: Path) -> list[str]:
    """核验实例文件声明的「写入者」真实存在且真的写它（《01_根系统治理》SOP 第 2 步）。

    声明了写入者却无人落笔，该真源在新环境永远是空壳，依赖它的下游全部静默失效——
    读者还会以为它已经被管起来了。本项把声明与实现的偏移机械检出，三种形态：
      `<tool>.py <symbol>`  → 工具存在且含该符号（符号通常是负责写入的函数名）
      `<tool>.py`（无符号）  → 工具存在且正文提到目标文件名
      `<name> Skill`        → Skill 目录存在且其 SKILL.md 提到目标文件名
    只声明人工/Agent 维护的不做机械核验——人是否落笔无法静态判定。
    """
    system = root / paths.SYSTEM_DIRNAME
    provenance = system / "tools" / "stamp_data_provenance.py"
    if not provenance.is_file():
        return []
    # 按显式文件路径加载，不走 sys.path：否则 sys.modules 里已有的同名模块会把
    # 被检工作区的声明表顶掉（体检可对任意 root 求值，不只是本仓库）。
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_lint_provenance", provenance)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        known = dict(module.KNOWN)
    except Exception:  # noqa: BLE001 - 声明表读不到时降级跳过，不阻断体检
        return []

    issues = []
    for target, meta in sorted(known.items()):
        # 1. source 指向控制面内的模板时，该模板必须真实存在（模板搬家后声明最易失修）
        for path_token in re.findall(r"\.entropaxis/[\w./-]+", meta.get("source", "")):
            if not (root / path_token).exists():
                issues.append(
                    f"[来源声明失效] 实例文件 {target} 声明来源 {path_token}，该路径不存在；"
                    "模板搬家或改名后必须同步 stamp_data_provenance.KNOWN。"
                )

        managed = meta.get("managed_by", "")
        # 2. 声明为工具写入：工具须存在，且含声明的符号（无符号则须提到目标文件名）
        for tool, symbol in re.findall(r"([\w_]+\.py)(?:\s+([A-Za-z_]\w+))?", managed):
            tool_path = system / "tools" / tool
            if not tool_path.is_file():
                issues.append(f"[写入者不存在] 实例文件 {target} 声明由 {tool} 维护，但 .entropaxis/tools/{tool} 不存在。")
                continue
            needle, kind = (symbol, "符号") if symbol else (target, "目标文件名")
            if needle not in tool_path.read_text(encoding="utf-8"):
                issues.append(
                    f"[写入者名不副实] 实例文件 {target} 声明由 {tool} 维护，"
                    f"但该工具正文不含{kind}「{needle}」——声明的写入者并不写它。"
                )
        # 3. 声明为 Skill 写入：Skill 须存在，且其 SKILL.md 提到目标文件名
        for skill in re.findall(r"([\w-]+)\s+Skill", managed):
            skill_md = system / "skills" / skill / "SKILL.md"
            if not skill_md.is_file():
                issues.append(f"[写入者不存在] 实例文件 {target} 声明由 {skill} Skill 维护，但该 Skill 不存在。")
            elif target not in skill_md.read_text(encoding="utf-8"):
                issues.append(
                    f"[写入者名不副实] 实例文件 {target} 声明由 {skill} Skill 维护，"
                    f"但其 SKILL.md 未提及 {target}——声明的写入者并不写它。"
                )
    return issues


def check_routing_integrity(root: Path) -> list[str]:
    """检查根入口动作矩阵、项目注册表及事务胶囊容器链条的完整性与连通性。"""
    issues = []

    # 1. 检查根 AGENTS.md 动作路由表中的每个规则目标文件真实存在
    root_agents = root / "AGENTS.md"
    if root_agents.exists():
        text = root_agents.read_text(encoding="utf-8")
        rule_refs = re.findall(r"`(\.entropaxis/rules/[^`]+)`", text)
        for ref in rule_refs:
            clean_ref = ref.split("#")[0].strip()
            if not (root / clean_ref).exists():
                issues.append(f"[根路由断链] 根 AGENTS.md 引用的规则文件 {ref} 不存在。")

    # 2. 检查 .entropaxis/data/templates/registry.md 中的每个项目主目录物理存在
    registry = root / paths.SYSTEM_DIRNAME / "data" / "templates" / "registry.md"
    if registry.exists():
        for line in registry.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("|") or line.startswith("| 项目 ID") or line.startswith("|---"):
                continue
            cols = [c.strip() for c in line.split("|")[1:-1]]
            if len(cols) >= 3:
                dir_cell = cols[2]
                dirs = re.findall(r"`([^`]+)`", dir_cell)
                for d in dirs:
                    d = d.strip()
                    if not d or "+" in d:
                        continue
                    proj_dir = root / d.rstrip("/")
                    if not proj_dir.is_dir():
                        issues.append(f"[注册表断链] .entropaxis/data/templates/registry.md 注册的项目目录 {d} 物理不存在。")

    # 2b. 反向校验：工作区根级目录须能在注册表主目录或排除规则中找到归属，
    # 否则新目录会游离于白名单之外而不被察觉（正向校验只查"注册的目录是否存在"，不查"存在的目录是否注册"）。
    # 注册表尚未登记任何项目时本项不阻断：收件方的工作区在初始化前就已经有自己的目录，
    # 拿注册表空表去判他"目录未注册"等于开箱即红。该初始态由第 15c 项以建议项报出。
    table_tops, exclude_tops = _registered_top_dirs(registry)
    if table_tops:
        known_tops = table_tops | exclude_tops
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name in known_tops:
                continue
            issues.append(
                f"[根目录未注册] {child.name}/ 既不在 .entropaxis/data/templates/registry.md 的项目映射表，也不在其排除规则中，"
                "需人工登记项目归属或补充排除规则（不得由 Agent 自行判断归属）。"
            )

    # 3. 检查事务胶囊目录命名结构（YYYYMMDD_主题）
    capsule_pattern = re.compile(r"^\d{8}_.+$")
    for d in root.glob("**/20[2-3][0-9][0-1][0-9][0-3][0-9]_*"):
        if not d.is_dir() or not _is_first_party(d, root):
            continue
        if not capsule_pattern.match(d.name):
            issues.append(f"[胶囊命名异常] 事务胶囊目录 {d.relative_to(root)} 不符合 YYYYMMDD_主题 规范。")
    for agents_doc in root.rglob("AGENTS.md"):
        if agents_doc == root / "AGENTS.md" or not _is_first_party(agents_doc, root):
            continue
        text = agents_doc.read_text(encoding="utf-8")
        refs = re.findall(r"`([^`]+AGENTS\.md)`", text) + re.findall(r"\[[^\]]*\]\(([^)\s]+AGENTS\.md)\)", text)
        if not refs:
            issues.append(f"[子入口无回链] {agents_doc.relative_to(root)} 缺少指向工作区根 AGENTS.md 的回链。")
        else:
            for ref in refs:
                target = (agents_doc.parent / ref).resolve()
                if not target.exists() or target != (root / "AGENTS.md").resolve():
                    issues.append(f"[子入口回链无效] {agents_doc.relative_to(root)} -> {ref} 未能正确定位到工作区根 AGENTS.md。")
        for bad_rule in ("开发通用规则", "开发项目联动规则", "知识库规则", "项目运行规则", "表达文风规则", "代码库重构与治理规则"):
            if bad_rule in text:
                issues.append(f"[子入口硬编码废弃规则] {agents_doc.relative_to(root)} 包含废弃规则名 {bad_rule}，应收敛至单一根回链。")
    return issues


def check_deliverable_naming(root: Path) -> list[str]:
    issues = []
    has_chinese_pattern = re.compile(r"[\u4e00-\u9fa5]")
    for d in root.glob("**/20[2-3][0-9][0-1][0-9][0-3][0-9]_*"):
        if not d.is_dir() or not _is_first_party(d, root):
            continue
        if not has_chinese_pattern.search(d.name):
            issues.append(
                f"[胶囊非中文命名] 交付物容器目录 {d.relative_to(root)} 缺少中文主题，违反《文件交付》第 2.1 节中文主命名铁律。"
            )
    return issues


def main() -> int:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    # --json 供 report_selfcheck.py 等下游消费，避免消费方去 parse 人读散文（《工具设计》3.3 结构化输出）
    as_json = "--json" in sys.argv
    fixed_claude_md = []
    if "--fix-claude-md" in sys.argv:
        fixed_claude_md = [str(p.relative_to(ROOT)) for p in fix_claude_md_thin_shell(ROOT)]
        if as_json:
            pass
        elif fixed_claude_md:
            print("已自动将以下项目的 CLAUDE.md 改写为标准薄壳（一行标题 + @AGENTS.md）：")
            for p in fixed_claude_md:
                print(f"  • {p}")
        elif verbose:
            print("✅ 未发现需要改写的 CLAUDE.md。")

    checks = [
        ("1. .entropaxis 结构完整性检查", check_system_layout, True),
        ("2. 根入口真源同步检查", check_system_entry_sync, True),
        ("3. .entropaxis 路由链接检查", check_system_markdown_links, True),
        ("4. 脚手架模板契约检查", check_system_templates, True),
        ("5. Skill 入口与触发元数据检查", check_system_skills, True),
        ("6. .entropaxis 工具语法检查", check_system_tools_compile, True),
        ("7. 路由完整性与胶囊容器检查", check_routing_integrity, True),
        ("8. 常驻层 Token 预算检查", check_resident_budget, False),
        ("9. 契约状态文件历史堆积检查", check_current_state_bloat, False),
        ("10. 规则单一真源去重检查", check_rule_deduplication, False),
        ("12. CLAUDE.md 薄壳纯净度检查", check_claude_md_thin_shell, False),
        ("13. rules/ 零系统绑定检查", check_rules_zero_system_binding, False),
        ("15c. 项目注册表登记进度检查", check_registry_population, False),
        ("15d. Skill 根系统独立性检查", check_skill_system_independence, True),
        ("17. 规则文件行数预算检查", check_rule_budget, False),
        ("18. 跨规则文件连续文本重复检查", check_rule_text_repetition, False),
        ("19. .entropaxis/data/ 实例文件来源标记检查", check_data_provenance, False),
        ("19b. 声明写入者存在性检查", check_declared_writers, True),
        ("20. .entropaxis/data/ 路径与来源映射检查", check_data_source_mapping, False),
        ("21. 结构化契约 schema 校验", check_schema_conformance, True),
        ("22. .entropaxis/data/ 实例声明落地检查", check_data_declaration_links, False),
        ("23. 交付物中文主命名检查", check_deliverable_naming, False),
        ("24. 语法税与 Token 经济性预算检查", check_syntax_tax_budget, True),
        ("25. 场景级联 Token 预算检查", check_scenario_cascade_budget, False),
    ]

    all_issues = []
    blocking_issues = []
    failed_checks = []

    if verbose and not as_json:
        print(f"🔍 开始对工作区进行健康度与上下文瘦身体检: {ROOT}\n" + "=" * 60)

    for title, fn, blocking in checks:
        issues = fn(ROOT)
        if issues:
            failed_checks.append((title, issues, blocking))
            all_issues.extend(issues)
            if blocking:
                blocking_issues.extend(issues)
            if verbose and not as_json:
                print(f"{title}: {'❌ 阻断' if blocking else '⚠️ 建议'} {len(issues)} 项")
                for issue in issues:
                    print(f"   • {issue}")
                print()
        elif verbose and not as_json:
            print(f"{title}: ✅ 正常\n")

    if as_json:
        print(json.dumps({
            "root": str(ROOT),
            "total_checks": len(checks),
            "blocking_count": len(blocking_issues),
            "advisory_count": len(all_issues) - len(blocking_issues),
            "passed": not blocking_issues,
            "fixed_claude_md": fixed_claude_md,
            "checks": [
                {"title": t, "blocking": b, "issues": i}
                for t, i, b in failed_checks
            ],
            "passed_checks": [
                t for t, _, _ in checks if t not in {ft for ft, _, _ in failed_checks}
            ],
        }, ensure_ascii=False, indent=2))
        return 1 if blocking_issues else 0

    if not verbose and failed_checks:
        for title, issues, blocking in failed_checks:
            print(f"{title}: {'❌ 阻断' if blocking else '⚠️ 建议'} {len(issues)} 项")
            for issue in issues:
                print(f"   • {issue}")

    if blocking_issues:
        print(f"❌ 体检失败：{len(blocking_issues)} 项 .entropaxis 控制面契约未满足。")
        return 1
    if not all_issues:
        print(f"🎉 工作区体检全绿：全部 {len(checks)} 项门禁通过！")
    else:
        print(f"💡 体检完成：共发现 {len(all_issues)} 项治理优化建议，请按需维护调整。")
    return 0
if __name__ == "__main__":
    sys.exit(main())

