#!/usr/bin/env python3
"""安全初始化时间线驱动的独立项目。"""

from __future__ import annotations

import argparse
import re
import tempfile
from datetime import datetime
from pathlib import Path


class ProjectInitError(Exception):
    """项目无法安全初始化。"""


TEMPLATE_FILES = {
    "AGENTS.template.md": "AGENTS.md",
    "CLAUDE.template.md": "CLAUDE.md",
    "项目总览.template.md": "_项目总览.md",
    "当前状态.template.md": "_契约/当前状态.md",
    "知识库索引.template.md": "_知识库/index.md",
}

DOCS_INDEX_TEMPLATE = "DocsIndex.template.md"
DOCS_DIRECTORIES = (
    "00_project",
    "01_research",
    "02_product",
    "03_design/mockups",
    "04_architecture/specs",
    "05_reports",
    "06_archive",
)


def _validate_name(name: str) -> str:
    if (
        name != name.strip()
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 for char in name)
    ):
        raise ProjectInitError("项目名必须是安全的单层目录名")
    return name


def _validate_date(value: str | None) -> str:
    value = value or datetime.now().strftime("%Y%m%d")
    try:
        datetime.strptime(value, "%Y%m%d")
    except (TypeError, ValueError) as error:
        raise ProjectInitError("开始日期必须使用 YYYYMMDD") from error
    return value


def _validate_dashboard_project_id(value: str) -> str:
    value = value.strip() or "未关联"
    if value != "未关联" and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
        raise ProjectInitError("Dashboard 项目 ID 格式无效")
    return value


def _render(path: Path, values: dict[str, str]) -> str:
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    if "{{" in text or "}}" in text:
        raise ProjectInitError(f"模板含未替换变量: {path}")
    return text


def init_project(
    name: str,
    *,
    workspace: Path,
    templates: Path,
    purpose_goal: str = "待补充",
    people: str = "待补充",
    next_event: str = "待定",
    materials: str = "无",
    project_knowledge: str = "待整理",
    shared_knowledge: str = "待整理",
    sensitivity: str = "普通内部",
    dashboard_project_id: str = "未关联",
    start: str | None = None,
    docs_layout: bool = False,
) -> Path:
    name = _validate_name(name)
    start = _validate_date(start)
    dashboard_project_id = _validate_dashboard_project_id(dashboard_project_id)
    workspace = workspace.expanduser().resolve()
    templates = templates.expanduser().resolve()

    if not workspace.is_dir():
        raise ProjectInitError(f"工作区不存在或不是目录: {workspace}")
    if not templates.is_dir():
        raise ProjectInitError(f"模板目录不存在: {templates}")

    target = workspace / name
    if target.exists():
        raise ProjectInitError(f"项目目录已存在，拒绝覆盖: {target}")

    # 动态计算从项目目录到工作区根的相对深度（目前 init 始终在 workspace/name，固定 1 级）
    depth = len((workspace / name).relative_to(workspace).parts)
    root_agents_path = "../" * depth + "AGENTS.md"

    values = {
        "项目名": name,
        "开始日期": start,
        "用途和目标": purpose_goal.strip() or "待补充",
        "相关人员": people.strip() or "待补充",
        "下一关键事件": next_event.strip() or "待定",
        "已有材料": materials.strip() or "无",
        "项目独有知识": project_knowledge.strip() or "待整理",
        "共享知识": shared_knowledge.strip() or "待整理",
        "敏感级别": sensitivity.strip() or "普通内部",
        "Dashboard项目ID": dashboard_project_id,
        "ROOT_AGENTS_PATH": root_agents_path,
    }
    rendered = {
        destination: _render(templates / source, values)
        for source, destination in TEMPLATE_FILES.items()
    }
    if docs_layout:
        rendered["docs/README.md"] = _render(
            templates / DOCS_INDEX_TEMPLATE,
            values,
        )

    with tempfile.TemporaryDirectory(prefix=".project-init-", dir=workspace) as temporary:
        staging = Path(temporary) / name
        (staging / "_契约").mkdir(parents=True)
        (staging / "_知识库" / "项目资料").mkdir(parents=True)
        if docs_layout:
            for directory in DOCS_DIRECTORIES:
                (staging / "docs" / directory).mkdir(parents=True)
        for destination, content in rendered.items():
            path = staging / destination
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        staging.replace(target)

    return target


def _parser() -> argparse.ArgumentParser:
    system_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="初始化独立的时间线项目")
    parser.add_argument("name", help="项目目录名")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=system_root.parent,
        help="项目工作区，默认是系统工程同级目录",
    )
    parser.add_argument("--purpose-goal", default="待补充", help="项目用途和目标")
    parser.add_argument("--people", default="待补充", help="负责人及相关人员")
    parser.add_argument("--next-event", default="待定", help="下一关键事件")
    parser.add_argument("--materials", default="无", help="已有材料位置")
    parser.add_argument("--project-knowledge", default="待整理", help="项目独有知识")
    parser.add_argument("--shared-knowledge", default="待整理", help="共享知识路径")
    parser.add_argument("--sensitivity", default="普通内部", help="敏感级别")
    parser.add_argument(
        "--dashboard-project-id",
        default="未关联",
        help="已确认的 Dashboard 项目 ID；不关联时省略",
    )
    parser.add_argument("--start", help="开始日期 YYYYMMDD")
    parser.add_argument(
        "--docs-layout",
        action="store_true",
        help="初始化分类 docs 布局（开发或文档密集型项目）",
    )
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    templates = Path(__file__).resolve().parent.parent / "templates"
    try:
        target = init_project(
            args.name,
            workspace=args.workspace,
            templates=templates,
            purpose_goal=args.purpose_goal,
            people=args.people,
            next_event=args.next_event,
            materials=args.materials,
            project_knowledge=args.project_knowledge,
            shared_knowledge=args.shared_knowledge,
            sensitivity=args.sensitivity,
            dashboard_project_id=args.dashboard_project_id,
            start=args.start,
            docs_layout=args.docs_layout,
        )
    except ProjectInitError as error:
        parser.error(str(error))
    print(target)


if __name__ == "__main__":
    main()
