#!/usr/bin/env python3
"""工作区健康度与上下文瘦身体检工具 (Workspace & Context Linter).

用于定期自动维护系统规则与工作区健康度，确保 Agent 在任何情况下保持上下文窗口极简、高信噪比。
执行方式:
  python3 .system/tools/lint_workspace.py
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

# 工作区常驻层预算
RESIDENT_MAX_LINES = 50
PROJECT_AGENTS_MAX_LINES = 60
MAX_CURRENT_STATE_LINES = 120

EXCLUDE_PATTERNS = (".system", "Archive", "repoes", "skills", "node_modules", "repo/dify", "graphify-out")

# rules/ 禁用具体业务系统名（检查 rules/ 零系统绑定）
FORBIDDEN_IN_RULES = ("内部操作手册", "Board-Platform联动规则", "internal-org.dev")

# .system 健康度：控制面可发现、可渲染、可执行的最小契约。
SYSTEM_REQUIRED_DIRECTORIES = ("root-configs", "rules", "templates", "tools", "skills", "tests")
SYSTEM_REQUIRED_FILES = (
    "AGENTS.md",
    "README.md",
    "root-configs/AGENTS.md",
    "root-configs/CLAUDE.md",
    "tools/bootstrap.py",
    "tools/init_project.py",
    "tools/init_app.py",
    "tools/lint_workspace.py",
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
    "Dashboard项目ID",
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


def check_claude_md_thin_shell(root: Path) -> list[str]:
    """所有 CLAUDE.md 必须是纯薄壳：一行标题 + 唯一一行 @AGENTS.md，无其他 import 或正文。
    例外：若 CLAUDE.md 是指向同目录 AGENTS.md 的符号链接（open-slide 等框架约定），视为等价薄壳。"""
    import os
    issues = []
    for cm in root.glob("**/CLAUDE.md"):
        cm_str = str(cm)
        if any(ex in cm_str for ex in ("Archive", "repoes", "node_modules", "repo/dify")):
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


def check_rules_zero_system_binding(root: Path) -> list[str]:
    """rules/ 不得出现具体业务系统绑定文件名或已删除文件的遗留引用。"""
    issues = []
    rules_dir = root / ".system" / "rules"
    if not rules_dir.exists():
        return issues
    for rf in rules_dir.glob("*.md"):
        try:
            content = rf.read_text(encoding="utf-8")
            for keyword in FORBIDDEN_IN_RULES:
                if keyword in content:
                    issues.append(
                        f"[规则系统绑定] {rf.name} 含禁用关键词「{keyword}」；rules/ 应为零系统绑定。"
                    )
        except Exception:
            pass
    return issues


def check_skill_symlink_health(root: Path) -> list[str]:
    """校验 Agent 安装目录中 internal-org-* skill 软链的有效性。"""
    issues = []
    true_source = root / ".system" / "skills"
    install_dirs = [
        Path.home() / ".claude" / "skills",
        Path.home() / ".pi" / "agent" / "skills",
    ]
    for install_dir in install_dirs:
        if not install_dir.exists():
            continue
        for entry in install_dir.iterdir():
            if not entry.name.startswith("internal-org-"):
                continue
            if entry.is_symlink():
                target = entry.resolve()
                expected = (true_source / entry.name).resolve()
                if target != expected:
                    issues.append(
                        f"[软链漂移] {entry} → {target}（应指向 {expected}）。运行 lint --fix 修复。"
                    )
                elif not target.exists():
                    issues.append(
                        f"[断链] {entry} → {target} 目标不存在。运行 lint --fix 修复。"
                    )
            else:
                issues.append(
                    f"[非软链] {entry} 是实体目录/文件，应改为指向 .system/skills/ 的软链。"
                )
    return issues


def check_registry_exists(root: Path) -> list[str]:
    """检查 .data/registry.md 是否存在（lint 白名单完备性依赖它）。"""
    registry = root / ".data" / "registry.md"
    if not registry.exists():
        return [
            "[注册表缺失] .data/registry.md 不存在，无法校验项目白名单；请先执行 init-project 或手动创建。"
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
    """检查根入口是否与 .system/root-configs 的唯一真源一致。"""
    system = root / ".system"
    issues = []
    for filename in ("AGENTS.md", "CLAUDE.md"):
        source = system / "root-configs" / filename
        target = root / filename
        if not source.is_file() or not target.is_file():
            continue
        if target.read_text(encoding="utf-8") != source.read_text(encoding="utf-8"):
            issues.append(
                f"[根入口漂移] {filename} 与 .system/root-configs/{filename} 内容不一致；运行 bootstrap.py 恢复。"
            )
    return issues


def check_system_markdown_links(root: Path) -> list[str]:
    """检查 .system 内 Markdown 显式本地链接不指向不存在的路径。"""
    system = root / ".system"
    issues = []
    link_pattern = re.compile(r"\[[^\]]*]\(([^)\s]+)(?:\s+[^)]*)?\)")
    for document in system.rglob("*.md"):
        for target in link_pattern.findall(document.read_text(encoding="utf-8")):
            target = target.strip("<>")
            if not target or target.startswith(("#", "/", "~", "http:", "https:", "mailto:")):
                continue
            path = target.split("#", 1)[0]
            base = root if document.parent == system / "root-configs" else document.parent
            if path and not (base / path).exists():
                issues.append(
                    f"[路由断链] {document.relative_to(root)} → {target} 不存在。"
                )
    return issues


def check_system_templates(root: Path) -> list[str]:
    """检查模板完备性与变量契约，保证两个脚手架可独立渲染。"""
    templates = root / ".system" / "templates"
    issues = []
    required = (
        "AGENTS.template.md",
        "CLAUDE.template.md",
        "README.template.md",
        "DECISIONS.template.md",
        "ChangeLog.template.md",
        "ReleaseNote.template.md",
        "知识库索引.template.md",
        "DocsIndex.template.md",
        "Product.template.md",
        "Tasks.template.md",
    )
    for filename in required:
        template = templates / filename
        if not template.is_file():
            issues.append(f"[模板缺失] .system/templates/{filename} 不存在。")
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
def main() -> int:
    print(f"🔍 开始对工作区进行健康度与上下文瘦身体检: {ROOT}\n" + "=" * 60)

    checks = [
        ("1. .system 结构完整性检查", check_system_layout, True),
        ("2. 根入口真源同步检查", check_system_entry_sync, True),
        ("3. .system 路由链接检查", check_system_markdown_links, True),
        ("4. 脚手架模板契约检查", check_system_templates, True),
        ("5. Skill 入口与触发元数据检查", check_system_skills, True),
        ("6. .system 工具语法检查", check_system_tools_compile, True),
        ("7. 常驻层 Token 预算检查", check_resident_budget, False),
        ("8. 契约状态文件历史堆积检查", check_current_state_bloat, False),
        ("9. 规则单一真源去重检查", check_rule_deduplication, False),
        ("10. Dashboard 看板任务健康度检查", check_dashboard_task_hygiene, False),
        ("11. CLAUDE.md 薄壳纯净度检查", check_claude_md_thin_shell, False),
        ("12. rules/ 零系统绑定检查", check_rules_zero_system_binding, False),
        ("13. Skill 软链健康度检查", check_skill_symlink_health, False),
        ("14. 项目注册表存在性检查", check_registry_exists, False),
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
