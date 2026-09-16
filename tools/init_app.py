#!/usr/bin/env python3
"""安全初始化软件工程应用代码仓库（专用于 02_开发/<app>/ 或独立代码仓库）。"""

from __future__ import annotations

import argparse
import tempfile
from datetime import datetime
from pathlib import Path

try:
    from . import paths
except ImportError:
    import paths


class AppInitError(Exception):
    """软件应用无法安全初始化。"""


APP_AGENTS_TEMPLATE = "AppAGENTS.template.md"
CLAUDE_TEMPLATE = "CLAUDE.template.md"
DOCS_INDEX_TEMPLATE = "DocsIndex.template.md"
PRODUCT_TEMPLATE = "Product.template.md"
TASKS_TEMPLATE = "Tasks.template.md"
CHANGELOG_TEMPLATE = "Changelog.template.md"
BENCHMARK_TEMPLATE = "Benchmark.template.md"
RELEASE_NOTE_TEMPLATE = "ReleaseNote.template.md"
def _validate_name(name: str) -> str:
    if (
        name != name.strip()
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 for char in name)
    ):
        raise AppInitError("应用名必须是安全的单层目录名")
    return name


def _validate_date(value: str | None) -> str:
    value = value or datetime.now().strftime("%Y%m%d")
    try:
        datetime.strptime(value, "%Y%m%d")
    except (TypeError, ValueError) as error:
        raise AppInitError("开始日期必须使用 YYYYMMDD") from error
    return value


def _render(path: Path, values: dict[str, str]) -> str:
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    if "{{" in text or "}}" in text:
        raise AppInitError(f"模板含未替换变量: {path}")
    return text


def init_app(
    name: str,
    *,
    target_dir: Path,
    templates: Path,
    purpose: str = "待补充",
    users: str = "企业内部研发与业务人员",
    start: str | None = None,
) -> Path:
    name = _validate_name(name)
    start = _validate_date(start)
    target_dir = target_dir.expanduser().resolve()
    templates = templates.expanduser().resolve()

    if not templates.is_dir():
        raise AppInitError(f"模板目录不存在: {templates}")

    app_path = target_dir / name if target_dir.name != name else target_dir
    if app_path.exists() and any(app_path.iterdir()):
        raise AppInitError(f"目标应用目录已存在且非空，拒绝覆盖: {app_path}")

    try:
        rel_path = app_path.relative_to(paths.WORKSPACE_ROOT)
        depth = len(rel_path.parts)
        root_agents_path = "../" * depth + "AGENTS.md"
    except Exception:
        root_agents_path = "../../../../AGENTS.md"

    values = {
        "项目名": name,
        "应用名": name,
        "开始日期": start,
        "产品定位与核心价值": purpose.strip() or "待补充",
        "用户与使用场景": users.strip() or "企业内部研发与业务人员",
        "ROOT_AGENTS_PATH": root_agents_path,
    }

    with tempfile.TemporaryDirectory(prefix=".app-init-", dir=app_path.parent if app_path.parent.exists() else None) as temporary:
        staging = Path(temporary) / name
        staging.mkdir(parents=True)
        (staging / "src").mkdir()
        (staging / "test").mkdir()
        (staging / "docs").mkdir()

        # `docs/` 从此处按实际工程事项创建 YYYYMMDD_主题 容器。

        (staging / "AGENTS.md").write_text(
            _render(templates / APP_AGENTS_TEMPLATE, values), encoding="utf-8"
        )
        (staging / "CLAUDE.md").write_text(
            _render(templates / CLAUDE_TEMPLATE, values), encoding="utf-8"
        )
        (staging / "docs" / "README.md").write_text(
            _render(templates / DOCS_INDEX_TEMPLATE, values), encoding="utf-8"
        )
        (staging / "PRODUCT.md").write_text(
            _render(templates / PRODUCT_TEMPLATE, values), encoding="utf-8"
        )
        (staging / "docs" / "Tasks.md").write_text(
            _render(templates / TASKS_TEMPLATE, values), encoding="utf-8"
        )
        for template, filename in (
            (CHANGELOG_TEMPLATE, "Changelog.md"),
            (BENCHMARK_TEMPLATE, "Benchmark.md"),
            (RELEASE_NOTE_TEMPLATE, "ReleaseNote.md"),
        ):
            (staging / "docs" / filename).write_text(
                _render(templates / template, values), encoding="utf-8"
            )

        if app_path.exists():
            for item in staging.iterdir():
                item.replace(app_path / item.name)
        else:
            staging.replace(app_path)

    return app_path


def _parser() -> argparse.ArgumentParser:
    system_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="初始化独立的软件工程代码仓库")
    parser.add_argument("name", help="软件应用名称")
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=Path("."),
        help="目标父目录（默认当前目录）",
    )
    parser.add_argument("--purpose", default="待补充", help="产品定位与核心价值")
    parser.add_argument("--users", default="企业内部研发与业务人员", help="用户与使用场景")
    parser.add_argument("--start", help="开始日期 YYYYMMDD")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    templates = Path(__file__).resolve().parent.parent / "templates" / "project"
    try:
        target = init_app(
            args.name,
            target_dir=args.target_dir,
            templates=templates,
            purpose=args.purpose,
            users=args.users,
            start=args.start,
        )
    except AppInitError as error:
        parser.error(str(error))
    print(target)


if __name__ == "__main__":
    main()
