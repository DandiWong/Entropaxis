#!/usr/bin/env python3
"""工作区健康度与上下文瘦身体检工具 (Workspace & Context Linter).

用于定期自动维护系统规则与工作区健康度，确保 Agent 在任何情况下保持上下文窗口极简、高信噪比。
执行方式:
  python3 .system/tools/lint_workspace.py
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

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

EXCLUDE_PATTERNS = (".system", "Archive", "repoes", "skills", "node_modules", "repo/dify", "graphify-out")

# 零系统绑定/零真实实体禁词按实例声明外置于 .data/rules/零系统绑定词表.md：
# 词表写死在这里，等于让"防止硬编码组织名"的检查本身成为控制面里唯一硬编码组织名的
# 文件，随版本库分发给每一个收件方（软件工程.md「检查按影响选」通用性维度「已知盲区」已记载该悖论）。
BINDING_WORDLIST = ".data/rules/零系统绑定词表.md"
# 控制面禁止硬编码本地调试端点：外部系统交互必须经声明外置的看板/服务 CLI
FORBIDDEN_HOST_PATTERN = re.compile(r"127\.0\.0\.1|localhost")

# .system 健康度：控制面可发现、可渲染、可执行的最小契约。
SYSTEM_REQUIRED_DIRECTORIES = ("entrypoints", "rules", "config", "schemas", "templates", "tools", "skills", "tests")
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
        p_str = str(p)
        if any(ex in p_str for ex in EXCLUDE_PATTERNS) or p == root_agents:
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
            if "Archive" in str(cs) or ".system" in str(cs):
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
    rules_dir = root / ".system" / "rules"
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
    rules_dir = root / ".system" / "rules"
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
    rules_dir = root / ".system" / "rules"
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
    """按 .system/schemas/ 校验结构化契约。

    此前 route_map 的字段集、Front Matter 的取值域、审计报告的问题标注格式，
    契约都只存在于各自解析器的正则里——改规则的人无从得知自己在破坏一个解析器。
    """
    tools = root / ".system" / "tools"
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
    return [f"[违反 schema] {e}" for e in (vs.check_route_map() + vs.check_audit_report_schema_selftest())]


def check_data_source_mapping(root: Path) -> list[str]:
    """双向核验 .data/ 路径与 .system/ 定义方的对应关系。

    `.data/` 按定义方分三个桶，路径本身即指向来源：
      .data/templates/X  ⟺ .system/templates/data/X.template.*
      .data/skills/<N>/* ⟺ .system/skills/<N>/
      .data/rules/*      ⟺ 由某条规则声明（具体哪条见文件头 source 字段）
    双向检查能同时抓出孤儿实例文件与失配模板，防结构随时间漂移。
    """
    issues = []
    data_dir, sys_dir = root / ".data", root / ".system"
    if not data_dir.is_dir():
        return issues

    tpl_dir = data_dir / "templates"
    if tpl_dir.is_dir():
        for p in sorted(tpl_dir.glob("*")):
            if not p.is_file() or p.suffix not in (".md", ".json"):
                continue
            expect = sys_dir / "templates" / "data" / f"{p.stem}.template{p.suffix}"
            if not expect.exists():
                issues.append(
                    f"[实例孤儿] .data/templates/{p.name} 找不到对应模板 {expect.relative_to(root)}；"
                    "它不是模板渲染产物，应移入 .data/rules/ 或 .data/skills/<名>/。"
                )

    # 反向：模板存在却无实例。拆分 templates/data 与 templates/project 后
    # 该目录内每个模板都必然对应一个 .data/templates/ 实例，可严格双向核验。
    sys_tpl = sys_dir / "templates" / "data"
    if sys_tpl.is_dir() and tpl_dir.is_dir():
        for t in sorted(sys_tpl.glob("*.template.*")):
            stem, suffix = t.name.split(".template", 1)
            if not (tpl_dir / f"{stem}{suffix}").exists():
                issues.append(
                    f"[实例未渲染] .system/templates/data/{t.name} 没有对应实例 "
                    f".data/templates/{stem}{suffix}；运行 `python3 .system/tools/bootstrap.py` 渲染。"
                )

    skl_dir = data_dir / "skills"
    if skl_dir.is_dir():
        for d in sorted(skl_dir.iterdir()):
            if d.is_dir() and not (sys_dir / "skills" / d.name).is_dir():
                issues.append(
                    f"[实例孤儿] .data/skills/{d.name}/ 找不到对应 Skill "
                    f".system/skills/{d.name}/；Skill 已删除时其实例配置应一并清理。"
                )

    for p in sorted(data_dir.glob("*")):
        if p.is_file() and p.suffix in (".md", ".json"):
            issues.append(
                f"[未归桶] .data/{p.name} 位于顶层。按来源归入 templates/（模板渲染）、"
                "rules/（规则声明）或 skills/<名>/（Skill 私有）。"
            )
    return issues


def check_data_provenance(root: Path) -> list[str]:
    """检查 .data/ 顶层实例文件是否声明来源与写入策略。

    `.data/` 是人工真源与实例配置所在地，缺少来源标记时人眼无法判断某文件从哪来、
    谁在维护、能否重写——这正是人工仲裁配置被 Agent 整体覆盖的前置条件。
    只查顶层 .md/.json；`credentials/` 及子目录含凭据，不读不列举。
    """
    issues = []
    data_dir = root / ".data"
    if not data_dir.is_dir():
        return issues
    # 只扫三个来源桶；credentials/ 与 docs/ 不读不列举（前者含凭据，后者是研究产物）
    for p in sorted(q for b in ("templates", "rules", "skills") for q in (data_dir / b).rglob("*")):
        if not p.is_file() or p.suffix not in (".md", ".json"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if p.suffix == ".json":
            try:
                ok = isinstance(json.loads(text).get("_meta"), dict)
            except (json.JSONDecodeError, AttributeError):
                ok = False
        else:
            ok = text.startswith("---\n") and "\npolicy:" in text.split("\n---", 2)[0]
        if not ok:
            issues.append(
                f"[实例文件缺来源标记] .data/{p.relative_to(data_dir)} 未声明 source/managed_by/policy。"
                "运行 `python3 .system/tools/stamp_data_provenance.py` 补盖。"
            )
    return issues


def _is_nested_git_repo_path(path: Path, root: Path) -> bool:
    """path 是否位于工作区根之外、自带独立 .git 的上游/第三方目录内（含 .system/ 自身）。
    与 registry.md「自带 .git 且非工作区成员的上游仓库不纳入注册表」的排除口径一致，
    防止把有独立代码/内容契约的嵌套仓库误判为待规范化的工作区项目薄壳。"""
    current = path.parent
    while True:
        if (current / ".git").exists():
            return True
        if current == root or current.parent == current:
            return False
        current = current.parent


def check_claude_md_thin_shell(root: Path) -> list[str]:
    """所有 CLAUDE.md 必须是纯薄壳：一行标题 + 唯一一行 @AGENTS.md，无其他 import 或正文。
    例外：若 CLAUDE.md 是指向同目录 AGENTS.md 的符号链接（open-slide 等框架约定），视为等价薄壳；
    嵌套独立 git 仓库（见 `_is_nested_git_repo_path`）视为上游内容，不纳入检查。"""
    issues = []
    for cm in root.glob("**/CLAUDE.md"):
        cm_str = str(cm)
        if any(ex in cm_str for ex in ("Archive", "repoes", "node_modules", "repo/dify")):
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
        if any(ex in cm_str for ex in ("Archive", "repoes", "node_modules", "repo/dify")):
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
            fixed.append(str(cm.relative_to(root)))
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
    停用，由调用方留痕告警，不静默假装通过（《01_根系统治理》演进准则第 2 条）。
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
    system = root / ".system"
    tracked = _tracked_files(system)
    forbidden = _forbidden_bindings(root)
    if not forbidden:
        # 降级必须留痕：静默跳过会让"检查通过"与"检查没跑"在输出上无法区分
        issues.append(
            f"[实体词表未声明] 未读到 {BINDING_WORDLIST}，本轮仅执行结构性端点检查，"
            "真实实体词检查已降级停用；新工作区请按《01_根系统治理》零系统绑定铁律补齐词表。"
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


def check_skill_symlink_health(root: Path) -> list[str]:
    """校验 Agent 安装目录中与工作区同名 Skill 软链的有效性。"""
    issues = []
    true_source = root / ".system" / "skills"
    if not true_source.is_dir():
        return issues
    local_skills = {d.name for d in true_source.iterdir() if d.is_dir()}
    install_dirs = [
        Path.home() / ".claude" / "skills",
        Path.home() / ".pi" / "agent" / "skills",
    ]
    for install_dir in install_dirs:
        if not install_dir.exists():
            continue
        for entry in install_dir.iterdir():
            if entry.name not in local_skills:
                continue
            expected = (true_source / entry.name).resolve()
            if entry.is_symlink():
                target = entry.resolve()
                if not target.exists():
                    issues.append(
                        f"[断链] {entry} → {target} 目标不存在。"
                    )
                elif target != expected:
                    issues.append(
                        f"[软链漂移] {entry} → {target}（工作区存在真源，应指向 {expected}）。"
                    )
            else:
                issues.append(
                    f"[非软链] {entry} 是实体目录/文件，工作区存在真源，建议改为指向 .system/skills/ 的软链。"
                )
    return issues


CREDENTIAL_VALUE_MARKERS = (
    "--token", "--password", "--secret", "--api-key", "--apikey", "--access-token",
    "-t ", "token=", "apikey=", "api_key=", "secret=", "password=", "bearer ",
)
# 字段名本身即凭证信号：值不含 "--token" 之类子串也一样阻断（第 5 轮实测抓到
# {"token": "abc123"}、{"headers": {"Authorization": "..."}} 两种绕过——按值扫描
# 找不到任何标志性子串，但字段名已经把意图写得很清楚）。
CREDENTIAL_KEY_MARKERS = ("token", "password", "secret", "apikey", "api_key", "authorization", "headers")
# ponytail: 子串/前缀匹配覆盖常见 CLI 凭证写法与字段命名；无法穷尽任意 shell 拼接形态
# （如包进 `sh -c "cmd --token x"` 的单个字符串——这类已用子串匹配覆盖，但更深的
# shell 语法混淆仍需真正的 shell parser），超出本检查的确定性范围——命中即阻断，
# 未命中不代表安全，只代表本检查没找到。


def _iter_nodes(value, path=()):
    """递归产出 (完整祖先字段名路径, 节点值)，覆盖 dict/list 的每一层——不只是字符串
    叶子。此前只传递"当前一层"的字段名，递归下探一层就把父字段名覆盖丢失：
    `{"token": {"value": "abc"}}` 会被看成字段名 "value"（无凭证特征）+ 值
    "abc"（也无凭证特征），"token" 这个真正暴露意图的字段名从未被检查过
    （第 6 轮实测抓到，R6-M2）。list 不增加路径段（列表项和其所属字段同属一路径）。
    """
    yield path, value
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _iter_nodes(v, path + (k,))
    elif isinstance(value, list):
        for v in value:
            yield from _iter_nodes(v, path)


def check_board_config_no_credentials(root: Path) -> list[str]:
    """看板联动.md「Provider 四态结果契约」前置约束：凭证不进配置正文。

    board_config.json 的 providers[*] 会被拼进 subprocess 直接执行或读取；任何字段
    （不限于 cli 数组）出现看起来像凭证参数的字符串，或字段名本身就是凭证类命名
    （token/password/secret/headers 等，即便值本身不含标志性子串、不是字符串类型），
    都等于把凭证写进磁盘配置明文，与凭证只经无回显交互录入、只存 .data/credentials/
    的口径冲突，一律阻断。JSON 解析失败按 fail-closed 处理：无法确认干净就不放行。
    """
    path = root / ".data" / "templates" / "board_config.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"[board_config.json 无法解析] {path}: {exc}；解析失败时不放行，需人工核实内容后再体检。"]
    issues = []
    for role, spec in (data.get("providers") or {}).items():
        for field_path, node in _iter_nodes(spec):
            # 只在"刚引入该字段名"这一层判定 key_hit（看最后一段），不对每层深度
            # 重复报告同一个祖先字段——既覆盖任意嵌套深度，又不产生一堆重复告警。
            key_hit = bool(field_path) and any(
                marker in field_path[-1].lower() for marker in CREDENTIAL_KEY_MARKERS
            )
            value_hit = isinstance(node, str) and any(marker in node.lower() for marker in CREDENTIAL_VALUE_MARKERS)
            if not (key_hit or value_hit):
                continue
            field_name = ".".join(field_path) if field_path else "<root>"
            reason = f"字段名 {field_name!r} 疑似凭证字段" if key_hit else f"字段 {field_name!r} 值含疑似凭证参数 {node!r}"
            issues.append(
                f"[Provider 凭证泄漏] .data/templates/board_config.json providers.{role} "
                f"{reason}；凭证只能经无回显交互录入并存 .data/credentials/，不得写入此文件。"
            )
    return issues


def check_registry_exists(root: Path) -> list[str]:
    """检查 .data/templates/registry.md 是否存在（lint 白名单完备性依赖它）。"""
    registry = root / ".data" / "templates" / "registry.md"
    if not registry.exists():
        return [
            "[注册表缺失] .data/templates/registry.md 不存在，无法校验项目白名单；请先执行 init-project 或手动创建。"
        ]
    return []


def check_dashboard_task_hygiene(root: Path) -> list[str]:
    """检查 Dashboard 数据库任务健康度（是否存在微观代码级任务或历史堆积）。"""
    issues = []
    db_path = root / "dashboard" / "dashboard.db"
    if not db_path.exists():
        return issues

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1. 检查活跃任务总数
        total_active = cur.execute(
            "SELECT count(*) as cnt FROM tasks WHERE stage IN ('plan', 'active', 'review')"
        ).fetchone()["cnt"]
        if total_active > 100:
            issues.append(
                f"[看板负载过高] 当前进行中/计划中任务共 {total_active} 条，建议评估是否有微观研发任务混入，保持高层看板纯净。"
            )

        # 2. 检查是否有超长未归档的已完成任务
        stale_done = cur.execute(
            "SELECT count(*) as cnt FROM tasks WHERE stage='done' AND completed_at < date('now', '-30 day')"
        ).fetchone()["cnt"]
        if stale_done > 20:
            issues.append(
                f"[归档沉降待处理] 存在 {stale_done} 条完成超过 30 天的已完结任务，确认是否需要清理或归档。"
            )
        conn.close()
    except Exception as e:
        issues.append(f"[数据库连接异常] 无法读取 dashboard.db: {e}")

    return issues




def check_system_layout(root: Path) -> list[str]:
    """检查 .system 控制面所需目录和入口文件。"""
    system = root / ".system"
    issues = []
    for directory in SYSTEM_REQUIRED_DIRECTORIES:
        if not (system / directory).is_dir():
            issues.append(f"[系统结构缺失] .system/{directory}/ 不存在。")
    for filename in SYSTEM_REQUIRED_FILES:
        if not (system / filename).is_file():
            issues.append(f"[系统入口缺失] .system/{filename} 不存在。")
    return issues


def check_system_entry_sync(root: Path) -> list[str]:
    """检查根入口是否与 .system/entrypoints 的唯一真源一致。"""
    system = root / ".system"
    issues = []
    for filename in ("AGENTS.md", "CLAUDE.md"):
        source = system / "entrypoints" / filename
        target = root / filename
        if not source.is_file() or not target.is_file():
            continue
        if target.read_text(encoding="utf-8") != source.read_text(encoding="utf-8"):
            issues.append(
                f"[根入口漂移] {filename} 与 .system/entrypoints/{filename} 内容不一致；运行 bootstrap.py 恢复。"
            )
    return issues


def _iter_local_links(root: Path):
    """遍历 .system 内 Markdown 的显式本地链接，产出 (文档, 原始 target, 解析后路径)。"""
    system = root / ".system"
    link_pattern = re.compile(r"\[[^\]]*]\(([^)\s]+)(?:\s+[^)]*)?\)")
    for document in system.rglob("*.md"):
        # Skill 包内随附的冻结参考文档（skills/<name>/references/**）不参与路由断链检查：
        # 其内部链接属于第三方/上游文档结构，不构成工作区路由契约。
        parts = document.relative_to(system).parts
        if len(parts) >= 3 and parts[0] == "skills" and parts[2] == "references":
            continue
        for target in link_pattern.findall(document.read_text(encoding="utf-8")):
            target = target.strip("<>")
            if not target or target.startswith(("#", "/", "~", "http:", "https:", "mailto:")):
                continue
            path = target.split("#", 1)[0]
            if not path:
                continue
            base = root if document.parent == system / "entrypoints" else document.parent
            yield document, target, (base / path)


def _points_into_data(path: Path, root: Path) -> bool:
    """判断链接目标是否落在 .data/ 实例面内（两侧同样 resolve，兼容 /tmp 软链前缀）。"""
    try:
        path.resolve().relative_to((root / ".data").resolve())
        return True
    except ValueError:
        return False


def check_system_markdown_links(root: Path) -> list[str]:
    """检查 .system 内 Markdown 显式本地链接不指向不存在的路径。

    指向 `.data/` 的实例声明引用交由第 22 项单独核验：`.data/rules/` 桶按定义无模板
    （见 `控制面布局.md`「.data/ 目录结构」），首次使用前必然不存在——把它算作阻断项，
    等于让分发出去的系统在新环境里开箱即红。
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
    """核验规则正文引用的 `.data/` 实例声明在本工作区是否已落地。

    `01_根系统治理.md` 审计流程第 2 步要求盘点"新初始化/分发场景下悬空的 .data/ 实例
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

    templates/ 按消费方分两个子目录：data/ 渲染到 .data/（bootstrap），
    project/ 是项目脚手架（init_project / init_app）。本检查只管后者。
    """
    templates = root / ".system" / "templates" / "project"
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
            issues.append(f"[模板缺失] .system/templates/project/{filename} 不存在。")
            continue
        text = template.read_text(encoding="utf-8")
        variables = set(re.findall(r"{{([^{}]+)}}", text))
        unknown = variables - SYSTEM_TEMPLATE_VARIABLES
        if unknown:
            issues.append(
                f"[模板变量未知] .system/templates/{filename} 包含未声明变量：{', '.join(sorted(unknown))}。"
            )
        if "{{" in re.sub(r"{{[^{}]+}}", "", text) or "}}" in re.sub(r"{{[^{}]+}}", "", text):
            issues.append(f"[模板变量失配] .system/templates/{filename} 含未闭合变量标记。")
    return issues


def check_system_skills(root: Path) -> list[str]:
    """检查每个系统 Skill 的入口与元数据名称。"""
    skills = root / ".system" / "skills"
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


def check_system_tools_compile(root: Path) -> list[str]:
    """编译 .system/tools 下脚本，阻止控制面工具语法损坏。"""
    tools = root / ".system" / "tools"
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
def check_routing_integrity(root: Path) -> list[str]:
    """检查根入口动作矩阵、项目注册表及主题胶囊容器链条的完整性与连通性。"""
    issues = []

    # 1. 检查根 AGENTS.md 动作路由表中的每个规则目标文件真实存在
    root_agents = root / "AGENTS.md"
    if root_agents.exists():
        text = root_agents.read_text(encoding="utf-8")
        rule_refs = re.findall(r"`(\.system/rules/[^`]+)`", text)
        for ref in rule_refs:
            clean_ref = ref.split("#")[0].strip()
            if not (root / clean_ref).exists():
                issues.append(f"[根路由断链] 根 AGENTS.md 引用的规则文件 {ref} 不存在。")

    # 2. 检查 .data/templates/registry.md 中的每个项目主目录物理存在
    registry = root / ".data" / "templates" / "registry.md"
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
                        issues.append(f"[注册表断链] .data/templates/registry.md 注册的项目目录 {d} 物理不存在。")

    # 3. 检查主题胶囊目录命名与结构（YYYYMMDD_主题）
    capsule_pattern = re.compile(r"^\d{8}_.+$")
    for d in root.glob("**/20[2-3][0-9][0-1][0-9][0-3][0-9]_*"):
        if any(ex in str(d) for ex in EXCLUDE_PATTERNS) or not d.is_dir():
            continue
        if not capsule_pattern.match(d.name):
            issues.append(f"[胶囊命名异常] 主题胶囊目录 {d.relative_to(root)} 不符合 YYYYMMDD_主题 规范。")


    # 4. 检查全工作区所有子项目 AGENTS.md 的工作区根回链连通性与零废弃规则
    for agents_doc in root.rglob("AGENTS.md"):
        if agents_doc == root / "AGENTS.md" or any(ex in str(agents_doc) for ex in EXCLUDE_PATTERNS):
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


def check_route_map_integrity(root: Path) -> list[str]:
    """检查确定性路由映射表 (route_map.json) 结构完整且目标文件真实存在。"""
    import json as _json

    issues = []
    route_map = root / ".system" / "config" / "route_map.json"
    if not route_map.is_file():
        return issues
    try:
        data = _json.loads(route_map.read_text(encoding="utf-8"))
    except _json.JSONDecodeError as error:
        return [f"[路由映射表解析失败] {route_map.relative_to(root)}: {error.msg}"]
    for entry in data.get("routes", []):
        mechanism = entry.get("mechanism", "<未命名机制>")
        if not entry.get("keywords"):
            issues.append(f"[路由映射缺关键词] 机制「{mechanism}」未声明 keywords。")
        for f in entry.get("files", []):
            if not (root / f).exists():
                issues.append(f"[路由映射断链] 机制「{mechanism}」引用的 {f} 不存在。")
    return issues


def main() -> int:
    if "--fix-claude-md" in sys.argv:
        fixed = fix_claude_md_thin_shell(ROOT)
        if fixed:
            print(f"🔧 已改写 {len(fixed)} 个 CLAUDE.md 为标准薄壳：")
            for f in fixed:
                print(f"   • {f}")
        else:
            print("✅ 未发现需要改写的 CLAUDE.md。")
        print()

    print(f"🔍 开始对工作区进行健康度与上下文瘦身体检: {ROOT}\n" + "=" * 60)

    checks = [
        ("1. .system 结构完整性检查", check_system_layout, True),
        ("2. 根入口真源同步检查", check_system_entry_sync, True),
        ("3. .system 路由链接检查", check_system_markdown_links, True),
        ("4. 脚手架模板契约检查", check_system_templates, True),
        ("5. Skill 入口与触发元数据检查", check_system_skills, True),
        ("6. .system 工具语法检查", check_system_tools_compile, True),
        ("7. 路由完整性与胶囊容器检查", check_routing_integrity, True),
        ("8. 常驻层 Token 预算检查", check_resident_budget, False),
        ("9. 契约状态文件历史堆积检查", check_current_state_bloat, False),
        ("10. 规则单一真源去重检查", check_rule_deduplication, False),
        ("11. Dashboard 看板任务健康度检查", check_dashboard_task_hygiene, False),
        ("12. CLAUDE.md 薄壳纯净度检查", check_claude_md_thin_shell, False),
        ("13. rules/ 零系统绑定检查", check_rules_zero_system_binding, False),
        ("14. Skill 软链健康度检查", check_skill_symlink_health, False),
        ("15. 项目注册表存在性检查", check_registry_exists, False),
        ("15b. 看板 Provider 凭证泄漏检查", check_board_config_no_credentials, True),
        ("16. 确定性路由映射表完整性检查", check_route_map_integrity, True),
        ("17. 规则文件行数预算检查", check_rule_budget, False),
        ("18. 跨规则文件连续文本重复检查", check_rule_text_repetition, False),
        ("19. .data/ 实例文件来源标记检查", check_data_provenance, False),
        ("20. .data/ 路径与来源映射检查", check_data_source_mapping, False),
        ("21. 结构化契约 schema 校验", check_schema_conformance, True),
        ("22. .data/ 实例声明落地检查", check_data_declaration_links, False),
    ]

    all_issues = []
    blocking_issues = []
    for title, fn, blocking in checks:
        issues = fn(ROOT)
        status = "✅ 正常" if not issues else f"{'❌ 阻断' if blocking else '⚠️ 建议'} {len(issues)} 项"
        print(f"{title}: {status}")
        for issue in issues:
            print(f"   • {issue}")
            all_issues.append(issue)
            if blocking:
                blocking_issues.append(issue)
        print()

    print("=" * 60)
    if blocking_issues:
        print(f"❌ 体检失败：{len(blocking_issues)} 项 .system 控制面契约未满足。")
        return 1
    if not all_issues:
        print("🎉 工作区体检完毕：所有控制面与治理规则均符合要求！")
    else:
        print(f"💡 体检完成：共发现 {len(all_issues)} 项治理优化建议，请按需维护调整。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

