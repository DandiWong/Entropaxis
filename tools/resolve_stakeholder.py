#!/usr/bin/env python3
"""按自然语言、项目 ID 或人员别名精准解析干系人矩阵，降低大模型定位时的上下文 Token 开销。

真源：.entropaxis/data/templates/stakeholders.yaml（契约 schemas/stakeholders.schema.json）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

try:
    from . import paths, project_registry
except ImportError:
    import paths
    import project_registry


class StakeholderError(Exception):
    """干系人解析异常。"""


def stakeholders_path(workspace_root: Path | None = None) -> Path:
    """返回 stakeholders.yaml 路径。"""
    root = workspace_root or paths.WORKSPACE_ROOT
    return root / paths.SYSTEM_DIRNAME / "data" / "templates" / "stakeholders.yaml"


def load_stakeholders_raw(workspace_root: Path | None = None) -> dict:
    """读取并解析 stakeholders.yaml。"""
    path = stakeholders_path(workspace_root)
    if not path.is_file():
        return {"people": [], "projects": {}}
    if yaml is None:
        raise StakeholderError("缺少 PyYAML 库，无法解析 stakeholders.yaml；请运行 pip install pyyaml")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise StakeholderError(f"解析 stakeholders.yaml 失败: {exc}") from exc
    if not isinstance(data, dict):
        raise StakeholderError("stakeholders.yaml 顶层必须是对象")
    return data


def load_people_map(workspace_root: Path | None = None) -> dict[str, dict]:
    """读取人员主库，返回 {person_id: person_info} 字典。"""
    raw = load_stakeholders_raw(workspace_root)
    people_map = {}
    for p in raw.get("people", []):
        if isinstance(p, dict) and p.get("id"):
            people_map[p["id"]] = p
    return people_map


def load_project_parents(workspace_root: Path | None = None) -> dict[str, str]:
    """从 registry.yaml 获取项目父子层级映射 {project_id: parent_id}。"""
    root = workspace_root or paths.WORKSPACE_ROOT
    parents = {}
    try:
        for p in project_registry.load_projects(root):
            if p.get("id") and p.get("parent"):
                parents[p["id"]] = p["parent"]
    except Exception:
        pass
    return parents


def resolve_person(query: str, workspace_root: Path | None = None) -> dict | None:
    """根据查询词（ID、全名、别名、转写昵称）精准消歧并定位人员。"""
    query = query.strip()
    if not query:
        return None

    people_map = load_people_map(workspace_root)
    lowered = query.lower()

    # 1. 精确匹配 ID
    for pid, p in people_map.items():
        if pid.lower() == lowered:
            return p

    # 2. 精确匹配标准全名
    for p in people_map.values():
        if p.get("name", "").lower() == lowered:
            return p

    # 3. 精确匹配别名
    for p in people_map.values():
        for alias in p.get("aliases", []):
            if alias.lower() == lowered:
                return p

    # 4. 包含匹配（按别名长度倒序，优先最长精准词）
    candidates: list[tuple[int, dict]] = []
    for p in people_map.values():
        matched_len = 0
        name = p.get("name", "")
        if name.lower() in lowered or lowered in name.lower():
            matched_len = max(matched_len, len(name))
        for alias in p.get("aliases", []):
            a_low = alias.lower()
            if a_low in lowered or lowered in a_low:
                matched_len = max(matched_len, len(alias))
        if matched_len > 0:
            candidates.append((matched_len, p))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    return None


def get_person_display(person_id: str | None, people_map: dict[str, dict], with_title: bool = False) -> str:
    """将 person_id 转化为规范展示文本（人名或人名+职务）。"""
    if not person_id:
        return ""
    p = people_map.get(person_id)
    if not p:
        return person_id
    name = p.get("name", person_id)
    if with_title and p.get("default_title"):
        return f"{name}({p['default_title']})"
    return name


def resolve_project_matrix(project_id: str, workspace_root: Path | None = None, inherit: bool = True) -> dict | None:
    """查询指定项目的干系人矩阵，支持沿 parent 自动继承缺省角色。"""
    raw = load_stakeholders_raw(workspace_root)
    projects = raw.get("projects", {})
    if project_id not in projects:
        return None

    people_map = load_people_map(workspace_root)
    parents = load_project_parents(workspace_root) if inherit else {}

    proj_data = dict(projects[project_id])
    proj_data["project_id"] = project_id

    # 继承链追溯
    inherited_fields = {}
    curr_parent = parents.get(project_id)
    visited = set()
    inheritable_keys = ("decision_maker", "business_owner", "project_manager", "product_manager", "rd_lead", "designer")

    while curr_parent and curr_parent in projects and curr_parent not in visited:
        visited.add(curr_parent)
        parent_data = projects[curr_parent]
        for key in inheritable_keys:
            if not proj_data.get(key) and parent_data.get(key) and key not in inherited_fields:
                proj_data[key] = parent_data[key]
                inherited_fields[key] = curr_parent
        curr_parent = parents.get(curr_parent)

    proj_data["_inherited_from"] = inherited_fields

    # 人员对象丰富化
    rich_data = dict(proj_data)
    for key in ("decision_maker", "business_owner", "project_manager", "product_manager", "rd_lead", "designer"):
        pid = proj_data.get(key)
        if pid:
            rich_data[f"{key}_display"] = get_person_display(pid, people_map, with_title=True)
            rich_data[f"{key}_name"] = get_person_display(pid, people_map, with_title=False)

    rd_engs = proj_data.get("rd_engineers") or []
    rich_data["rd_engineers_display"] = [get_person_display(pid, people_map, with_title=False) for pid in rd_engs]

    return rich_data


def find_person_roles(person_id: str, workspace_root: Path | None = None) -> list[dict]:
    """反查某人在所有项目中的角色分工。"""
    raw = load_stakeholders_raw(workspace_root)
    projects = raw.get("projects", {})
    results = []

    for pid, pdata in projects.items():
        matched_roles = []
        for role_key in ("decision_maker", "business_owner", "project_manager", "product_manager", "rd_lead", "designer"):
            if pdata.get(role_key) == person_id:
                matched_roles.append(role_key)
        if person_id in (pdata.get("rd_engineers") or []):
            matched_roles.append("rd_engineer")
        for mgroup, members in (pdata.get("members") or {}).items():
            if isinstance(members, list) and person_id in members:
                matched_roles.append(mgroup)
        if matched_roles:
            results.append({
                "project_id": pid,
                "project_name": pdata.get("name", pid),
                "roles": matched_roles,
            })
    return results


def format_matrix_text(matrix: dict) -> str:
    """格式化为高密度纯文本 (~25 tokens)。"""
    pid = matrix["project_id"]
    parts = []
    if matrix.get("decision_maker_display"):
        parts.append(f"决策人: {matrix['decision_maker_display']}")
    if matrix.get("business_owner_display"):
        parts.append(f"业务: {matrix['business_owner_display']}")
    if matrix.get("project_manager_name"):
        parts.append(f"PM: {matrix['project_manager_name']}")
    if matrix.get("product_manager_name"):
        parts.append(f"PO: {matrix['product_manager_name']}")
    if matrix.get("rd_lead_name"):
        parts.append(f"研发负责人: {matrix['rd_lead_name']}")
    if matrix.get("rd_engineers_display"):
        parts.append(f"研发: {', '.join(matrix['rd_engineers_display'])}")
    if matrix.get("designer_display"):
        parts.append(f"设计: {matrix['designer_display']}")
    if matrix.get("external_stakeholders"):
        ext_list = [f"{e['name']}({e.get('org', '')} {e.get('role', '')})".strip() for e in matrix["external_stakeholders"]]
        parts.append(f"外部合作: {', '.join(ext_list)}")
    return f"[{pid}] " + " | ".join(parts)


def format_person_text(person: dict, roles: list[dict]) -> str:
    """格式化人员详情与项目分工为高密度纯文本。"""
    title_part = f"({person['default_title']})" if person.get("default_title") else ""
    org_part = f" [{person.get('org', '')}]" if person.get("org") else ""
    header = f"{person['name']}{title_part}{org_part}"
    if person.get("aliases"):
        header += f" 别名: {', '.join(person['aliases'])}"

    if not roles:
        return f"{header}\n  暂无关联项目角色"

    role_lines = []
    for r in roles:
        role_lines.append(f"{r['project_id']} ({', '.join(r['roles'])})")
    return f"{header}\n  关联项目: " + " | ".join(role_lines)


def lint_stakeholders(workspace_root: Path | None = None) -> list[str]:
    """静态检查 stakeholders.yaml 的完整性、引用有效性与孤立引用。"""
    path = stakeholders_path(workspace_root)
    if not path.is_file():
        return ["[底册缺失] 未找到 stakeholders.yaml，请运行 bootstrap 渲染"]
    issues = []
    try:
        raw = load_stakeholders_raw(workspace_root)
    except StakeholderError as exc:
        return [f"[无法加载] {exc}"]

    people = raw.get("people", [])
    projects = raw.get("projects", {})

    # 1. 检查人员 ID 唯一性与格式
    seen_pids = set()
    all_aliases = {}
    for p in people:
        pid = p.get("id")
        if not pid:
            issues.append("[人员缺少 ID] people 列表中存在未声明 id 的项")
            continue
        if pid in seen_pids:
            issues.append(f"[人员 ID 重复] {pid}")
        seen_pids.add(pid)

        for a in p.get("aliases", []):
            a_low = a.lower()
            if a_low in all_aliases and all_aliases[a_low] != pid:
                issues.append(f"[别名冲突] 别名 '{a}' 同时被 {all_aliases[a_low]} 和 {pid} 登记")
            all_aliases[a_low] = pid

    # 2. 检查项目矩阵中的人员引用真实性
    root = workspace_root or paths.WORKSPACE_ROOT
    registry_pids = set()
    try:
        registry_pids = {p["id"] for p in project_registry.load_projects(root)}
    except Exception:
        pass

    for proj_id, pdata in projects.items():
        if registry_pids and proj_id not in registry_pids:
            issues.append(f"[未登记项目] projects 包含未在 registry.yaml 登记的项目 ID: '{proj_id}'")

        for role_key in ("decision_maker", "business_owner", "project_manager", "product_manager", "rd_lead", "designer"):
            ref_pid = pdata.get(role_key)
            if ref_pid and ref_pid not in seen_pids:
                issues.append(f"[人员引用断链] 项目 '{proj_id}' 的 {role_key} 引用了未在 people 声明的人员 ID: '{ref_pid}'")

        for ref_pid in pdata.get("rd_engineers") or []:
            if ref_pid not in seen_pids:
                issues.append(f"[人员引用断链] 项目 '{proj_id}' 的 rd_engineers 引用了未在 people 声明的人员 ID: '{ref_pid}'")

        for mgroup, members in (pdata.get("members") or {}).items():
            if isinstance(members, list):
                for ref_pid in members:
                    if ref_pid not in seen_pids:
                        issues.append(f"[人员引用断链] 项目 '{proj_id}' 的 members.{mgroup} 引用了未在 people 声明的人员 ID: '{ref_pid}'")

    return issues


def _truncate_list(items: list, limit: int, label: str) -> list:
    """按 CLI 列表上限截断，并在 stderr 提示可显示全部。"""
    if limit <= 0 or len(items) <= limit:
        return items
    print(f"⚠️ {label}共 {len(items)} 项，仅显示前 {limit} 项；使用 --limit 0 显示全部。", file=sys.stderr)
    return items[:limit]


def _run_cli(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """执行已解析的 CLI 请求。"""
    ws_root = Path(args.workspace_root).resolve() if args.workspace_root else None

    if args.lint:
        issues = lint_stakeholders(ws_root)
        if not issues:
            print("✅ 干系人与人员主库契约校验通过，无断链或冲突引用。")
            return 0
        print(f"❌ 发现 {len(issues)} 处干系人定义问题：", file=sys.stderr)
        for issue in issues:
            print(f"  • {issue}", file=sys.stderr)
        return 1

    if args.list:
        raw = load_stakeholders_raw(ws_root)
        project_ids = _truncate_list(list(raw.get("projects", {})), args.limit, "项目列表")
        matrices = [
            matrix for pid in project_ids
            if (matrix := resolve_project_matrix(pid, ws_root, inherit=True))
        ]
        for matrix in matrices:
            print(format_matrix_text(matrix))
        return 0

    if args.list_people:
        raw = load_stakeholders_raw(ws_root)
        people = _truncate_list(raw.get("people", []), args.limit, "人员列表")
        for p in people:
            print(format_person_text(p, find_person_roles(p["id"], ws_root)))
        return 0

    # 显式按人查询
    if args.person_query:
        person = resolve_person(args.person_query, ws_root)
        if not person:
            print(f"❌ 错误原因：未找到与 '{args.person_query}' 匹配的人员或别名。\n👉 修复建议：检查 stakeholders.yaml 中的 people 列表或添加其别名。", file=sys.stderr)
            return 2
        print(format_person_text(person, find_person_roles(person["id"], ws_root)))
        return 0

    # 显式按项目查询
    if args.project_query:
        matrix = resolve_project_matrix(args.project_query, ws_root)
        if not matrix:
            print(f"❌ 错误原因：未在 stakeholders.yaml 中找到项目 '{args.project_query}'。\n👉 修复建议：检查 stakeholders.yaml 中的 projects 定义或运行 --list 查看已登记项目。", file=sys.stderr)
            return 2
        print(format_matrix_text(matrix))
        return 0

    # 自动推断查询类型
    if not args.query:
        parser.print_help()
        return 1

    query = args.query.strip()
    # 优先查项目矩阵
    matrix = resolve_project_matrix(query, ws_root)
    if matrix:
        print(format_matrix_text(matrix))
        return 0

    # 次选查人员
    person = resolve_person(query, ws_root)
    if person:
        print(format_person_text(person, find_person_roles(person["id"], ws_root)))
        return 0

    print(f"❌ 错误原因：查询词 '{query}' 既未命中任何已登记项目，也未命中人员或别名。\n👉 修复建议：检查 .entropaxis/data/templates/stakeholders.yaml，确认项目 ID 或人员姓名/别名是否登记。", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按项目或人名别名精准解析干系人矩阵与人员主库")
    parser.add_argument("query", nargs="?", default="", help="项目 ID、人名或别名查询词")
    parser.add_argument("--project", "-p", dest="project_query", help="显式指定按项目查询")
    parser.add_argument("--person", "-u", dest="person_query", help="显式指定按人名/别名查询")
    parser.add_argument("--list", action="store_true", help="列出全部项目的干系人矩阵")
    parser.add_argument("--list-people", action="store_true", help="列出全部主库人员")
    parser.add_argument("--limit", type=int, default=20, help="列表最多显示的条数（0 或负数不截断）")
    parser.add_argument("--lint", action="store_true", help="执行干系人完整性与断链检查")
    parser.add_argument("--workspace-root", default=None, help="工作区根目录路径（可选）")

    args = parser.parse_args(argv)
    try:
        return _run_cli(args, parser)
    except StakeholderError as exc:
        print(f"❌ 错误原因：{exc}\n👉 修复建议：检查 stakeholders.yaml 的格式、路径与依赖后重试。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
