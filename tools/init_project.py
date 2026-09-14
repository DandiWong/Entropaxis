#!/usr/bin/env python3
"""安全初始化通用业务/综合项目（标准 4 域 + 2 契约 + RawInput + Archive 架构）。"""

from __future__ import annotations

import argparse
import re
import tempfile
from datetime import datetime
from pathlib import Path

try:
    from . import paths
except ImportError:
    import paths


class ProjectInitError(Exception):
    """项目无法安全初始化。"""


TEMPLATE_FILES = {
    "AGENTS.template.md": "AGENTS.md",
    "CLAUDE.template.md": "CLAUDE.md",
    "README.template.md": "README.md",
    "DECISIONS.template.md": "01_项目管理/DECISIONS.md",
    "知识库索引.template.md": "_知识库/index.md",
}

# 标准 4 域子目录架构
DOMAIN_DIRECTORIES = (
    "01_项目管理/01_资料",
    "01_项目管理/02_调研评估",
    "01_项目管理/03_会议决策",
    "01_项目管理/04_价值回收",
    "02_产品设计/01_需求清单",
    "02_产品设计/02_原型Demo",
    "02_产品设计/03_设计规范",
    "03_工程研发",
    "04_运营增长/01_上线发布",
    "04_运营增长/02_培训",
    "04_运营增长/03_营销推广",
    "04_运营增长/04_数据",
    "04_运营增长/05_用户反馈",
    "Archive",
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


def _validate_path(value: str | None) -> str:
    """校验父目录相对路径，逐段套用单层目录名的安全判据。

    子项目可以嵌套在父目录下（`--path 04A/05B`），也可以与父项目平铺在工作区根
    （不传 `--path`）。两种布局都合法，因此路径是显式入参而非从父项目 ID 推断。
    """
    if value is None or not value.strip():
        return ""
    parts = [segment for segment in value.strip().replace("\\", "/").split("/") if segment]
    for segment in parts:
        _validate_name(segment)
    return "/".join(parts)


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
        raise ProjectInitError("看板项目 ID 格式无效")
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
    parent: str = "",
    path: str | None = None,
) -> Path:
    name = _validate_name(name)
    path = _validate_path(path)
    start = _validate_date(start)
    dashboard_project_id = _validate_dashboard_project_id(dashboard_project_id)
    workspace = workspace.expanduser().resolve()
    templates = templates.expanduser().resolve()

    if not workspace.is_dir():
        raise ProjectInitError(f"工作区不存在或不是目录: {workspace}")
    if not templates.is_dir():
        raise ProjectInitError(f"模板目录不存在: {templates}")

    rel_dir = f"{path}/{name}" if path else name
    if path and not (workspace / path).is_dir():
        raise ProjectInitError(f"父目录不存在，先建父项目或修正 --path: {workspace / path}")
    target = workspace / rel_dir
    if target.exists():
        raise ProjectInitError(f"项目目录已存在，拒绝覆盖: {target}")

    # 动态计算从项目目录到工作区根的相对深度
    depth = len(Path(rel_dir).parts)
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
        "看板项目ID": dashboard_project_id,
        "ROOT_AGENTS_PATH": root_agents_path,
    }
    rendered = {
        destination: _render(templates / source, values)
        for source, destination in TEMPLATE_FILES.items()
    }

    with tempfile.TemporaryDirectory(prefix=".project-init-", dir=workspace / path if path else workspace) as temporary:
        staging = Path(temporary) / name
        # 1. 知识库与暂存投递箱
        (staging / "_知识库" / "项目资料").mkdir(parents=True)
        (staging / "RawInput").mkdir(parents=True)
        (staging / "RawInput" / ".gitkeep").write_text("", encoding="utf-8")

        # 2. 标准 4 域目录
        for directory in DOMAIN_DIRECTORIES:
            (staging / directory).mkdir(parents=True, exist_ok=True)


        # 4. 写入渲染模板
        for destination, content in rendered.items():
            path = staging / destination
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        staging.replace(target)

    register_project(workspace, name, dashboard_project_id, rel_dir=rel_dir, parent=parent)
    return target


def register_project(
    workspace: Path,
    name: str,
    dashboard_project_id: str = "未关联",
    rel_dir: str | None = None,
    parent: str = "",
) -> bool:
    """把新项目追加进 `.entropaxis/data/templates/registry.md` 映射表，返回是否发生写入。

    注册表正文声明本工具所属 Skill 是它的唯一写入者，但此前无人真的写——注册表因此在
    新工作区里永远是空表，而它是《项目组织》的项目归属唯一映射基准，也是体检第 7 项
    反向校验的依据。缺这一步，新机器上装好系统后项目索引永远建不起来。

    写入遵守 `data/` 写入规约：只追加一行，已登记同名项目即跳过，不重写既有内容；
    注册表缺失（尚未 bootstrap）时静默跳过，不阻断立项本身。
    """
    registry = workspace / paths.SYSTEM_DIRNAME / "data" / "templates" / "registry.md"
    if not registry.is_file():
        return False
    try:
        text = registry.read_text(encoding="utf-8")
    except OSError:
        return False
    rel_dir = (rel_dir or name).strip("/")
    if f"`{rel_dir}/`" in text:
        return False

    board = dashboard_project_id.strip() or "未关联"
    mapping = "未关联" if board == "未关联" else f"main={board}"
    row = f"| {name} | {name} | `{rel_dir}/` | {parent} | | {mapping} | |\n"
    # 锚到「项目 ID」表头下方的分隔行：文件里可能不止一张表，按表头定位而非取首个分隔行。
    # 占位行（（待填写））留在原处，新项目追加在它前面——不动人工内容，也不依赖占位行是否还在。
    lines = text.splitlines(keepends=True)
    header = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("| 项目 ID")), None)
    if header is None or header + 1 >= len(lines) or not lines[header + 1].lstrip().startswith("|-"):
        return False
    lines.insert(header + 2, row)
    try:
        registry.write_text("".join(lines), encoding="utf-8")
    except OSError:
        return False
    return True


def _parser() -> argparse.ArgumentParser:
    system_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="初始化通用业务/综合项目工作区（5 域 + 2 契约 + RawInput）")
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
        help="已确认的看板项目 ID；不关联时省略",
    )
    parser.add_argument("--start", help="开始日期 YYYYMMDD")
    parser.add_argument("--parent", default="", help="父项目的「项目 ID」；顶层项目省略")
    parser.add_argument(
        "--path",
        help="父目录相对路径（如 04A/05B）；嵌套布局才需要，平铺布局省略",
    )
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    templates = Path(__file__).resolve().parent.parent / "templates" / "project"
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
            parent=args.parent,
            path=args.path,
        )
    except ProjectInitError as error:
        parser.error(str(error))
    print(target)


if __name__ == "__main__":
    main()
