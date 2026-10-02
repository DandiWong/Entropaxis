#!/usr/bin/env python3
"""安全初始化软件工程应用代码仓库（专用于 03_工程研发/<app>/ 或独立代码仓库）。

已有代码的仓库用 --entry-only 只补 AGENTS.md / CLAUDE.md，已存在的文件跳过不动。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
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


ROOT_PATH_UNKNOWN = "待补充：工作区根 AGENTS.md 的相对路径"
UNFILLED = "待补充"
GITIGNORE = ".env\n.venv/\n__pycache__/\nnode_modules/\n.DS_Store\n"
TIMELINE_HEADING = "## 2. 关键里程碑与时间线"


def _candidate(command: str) -> str:
    return f"`{command}`（探测候选，待确认）"


def detect_test_commands(app_path: Path) -> dict[str, str]:
    """按仓库里已有的文件给出分层测试命令候选；探测不到的留待补充，不猜默认值。"""
    found = {"单元测试命令": UNFILLED, "集成测试命令": UNFILLED, "全量测试命令": UNFILLED, "效果验证命令": UNFILLED}
    if not app_path.is_dir():
        return found
    has_py_tests = any(app_path.glob("test*/**/test_*.py"))
    if has_py_tests or any((app_path / f).is_file() for f in ("pytest.ini", "pyproject.toml", "setup.cfg")):
        found["单元测试命令"] = _candidate("python3 -m pytest <相关测试文件>")
        found["全量测试命令"] = _candidate("python3 -m pytest")
    package = app_path / "package.json"
    if package.is_file():
        try:
            script = json.loads(package.read_text(encoding="utf-8")).get("scripts", {}).get("test")
        except (json.JSONDecodeError, AttributeError):
            script = None
        if script:
            found["全量测试命令"] = _candidate("npm test")
    has_mjs_tests = any(app_path.glob("test*/**/*.test.mjs")) or any(app_path.glob("test*/test_*.mjs"))
    if found["单元测试命令"] == UNFILLED and has_mjs_tests:
        found["单元测试命令"] = _candidate("node --test <相关测试文件>")
    if any(app_path.glob("Dockerfile")) or any(app_path.glob("deploy/**/Dockerfile")):
        found["集成测试命令"] = "存在 Dockerfile：确认集成测试是否须在镜像内跑（探测候选，待确认）"
    return found


def pending_lines(agents_md: Path) -> list[int]:
    """入口里仍待填的行号。代码仓库自带 .git，不在工作区体检范围内，只能由脚手架当场提示。"""
    if not agents_md.is_file():
        return []
    lines = agents_md.read_text(encoding="utf-8").splitlines()
    return [i for i, line in enumerate(lines, 1) if UNFILLED in line or "探测候选" in line]


def _root_agents_path(app_path: Path) -> str:
    try:
        depth = len(app_path.relative_to(paths.WORKSPACE_ROOT).parts)
    except ValueError:
        # ponytail: 工作区外无从推算，留可见待填标记，不写死猜测路径
        return ROOT_PATH_UNKNOWN
    return "../" * depth + "AGENTS.md"


def _git_init(app_path: Path) -> str:
    """新仓库执行 git init；已在某个工作树内或未装 git 时跳过并说明。不提交。"""
    if shutil.which("git") is None:
        return "未安装 git，跳过 git init"
    inside = subprocess.run(
        ["git", "-C", str(app_path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    if inside.returncode == 0:
        return "已位于现有 Git 工作树内，跳过 git init"
    subprocess.run(["git", "init", "-q", str(app_path)], check=True)
    return "已 git init（未提交）"


def record_in_parent(parent: Path, app_path: Path, start: str, action: str) -> str:
    """在上级业务项目 README 的时间线追加一条；README 或时间线章节缺失时跳过并说明。"""
    readme = parent.expanduser().resolve() / "README.md"
    if not readme.is_file():
        return f"上级项目无 README.md，未登记: {readme}"
    try:
        rel = app_path.expanduser().resolve().relative_to(readme.parent)
    except ValueError:
        return f"代码仓库不在上级项目目录内，未登记: {app_path}"
    lines = readme.read_text(encoding="utf-8").splitlines()
    if TIMELINE_HEADING not in lines:
        return f"上级项目 README 缺少「{TIMELINE_HEADING[6:]}」章节，未登记"
    entry = f"- `{start}`：{action}代码仓库 `{rel.as_posix()}/` · 状态: 已完成"
    if entry in lines:
        return "上级项目时间线已有此条，跳过"
    index = lines.index(TIMELINE_HEADING) + 1
    while index < len(lines) and not lines[index].startswith("## ") and lines[index].strip() != "---":
        index += 1
    while lines[index - 1].strip() == "":
        index -= 1
    lines.insert(index, entry)
    readme.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"已登记到上级项目时间线: {readme}"


def init_app(
    name: str,
    *,
    target_dir: Path,
    templates: Path,
    purpose: str = "待补充",
    users: str = "企业内部研发与业务人员",
    start: str | None = None,
    entry_only: bool = False,
    git_init: bool = True,
    notes: list[str] | None = None,
) -> Path:
    """创建代码仓库骨架；entry_only=True 时只给已有目录补入口文件。过程说明追加到 notes。"""
    name = _validate_name(name)
    start = _validate_date(start)
    target_dir = target_dir.expanduser().resolve()
    templates = templates.expanduser().resolve()
    notes = notes if notes is not None else []

    if not templates.is_dir():
        raise AppInitError(f"模板目录不存在: {templates}")

    app_path = target_dir / name if target_dir.name != name else target_dir
    if entry_only and not app_path.is_dir():
        raise AppInitError(f"--entry-only 只用于已有目录，目录不存在: {app_path}")
    if not entry_only and app_path.exists() and any(app_path.iterdir()):
        raise AppInitError(f"目标应用目录已存在且非空，拒绝覆盖（已有仓库请用 --entry-only）: {app_path}")

    values = {
        "项目名": name,
        "应用名": name,
        "开始日期": start,
        "产品定位与核心价值": purpose.strip() or "待补充",
        "用户与使用场景": users.strip() or "企业内部研发与业务人员",
        "ROOT_AGENTS_PATH": _root_agents_path(app_path),
        **detect_test_commands(app_path),
    }
    if values["ROOT_AGENTS_PATH"] == ROOT_PATH_UNKNOWN:
        notes.append("目标不在工作区内，AGENTS.md 的根入口路径留待补充")

    if entry_only:
        for template, filename in ((APP_AGENTS_TEMPLATE, "AGENTS.md"), (CLAUDE_TEMPLATE, "CLAUDE.md")):
            target = app_path / filename
            if target.exists():
                notes.append(f"{filename} 已存在，跳过不动")
            else:
                target.write_text(_render(templates / template, values), encoding="utf-8")
                notes.append(f"已生成 {filename}")
        _note_pending(app_path, notes)
        return app_path

    with tempfile.TemporaryDirectory(prefix=".app-init-", dir=app_path.parent if app_path.parent.exists() else None) as temporary:
        staging = Path(temporary) / name
        staging.mkdir(parents=True)
        (staging / "src").mkdir()
        (staging / "test").mkdir()
        (staging / "docs").mkdir()

        # `docs/` 从此处按实际工程事项创建 YYYYMMDD_主题 容器。

        (staging / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
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

    if git_init:
        notes.append(_git_init(app_path))
    _note_pending(app_path, notes)
    return app_path


def _note_pending(app_path: Path, notes: list[str]) -> None:
    pending = pending_lines(app_path / "AGENTS.md")
    if pending:
        notes.append(f"AGENTS.md 第 {', '.join(map(str, pending))} 行待确认或补充")


def _parser() -> argparse.ArgumentParser:
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
    parser.add_argument("--entry-only", action="store_true", help="已有代码仓库：只补 AGENTS.md / CLAUDE.md，已存在的跳过")
    parser.add_argument("--no-git", action="store_true", help="新建时不执行 git init")
    parser.add_argument("--parent", type=Path, help="上级业务项目目录：在其 README 时间线登记本仓库")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    templates = Path(__file__).resolve().parent.parent / "templates" / "project"
    notes: list[str] = []
    try:
        target = init_app(
            args.name,
            target_dir=args.target_dir,
            templates=templates,
            purpose=args.purpose,
            users=args.users,
            start=args.start,
            entry_only=args.entry_only,
            git_init=not args.no_git,
            notes=notes,
        )
    except AppInitError as error:
        parser.error(str(error))
    if args.parent:
        notes.append(record_in_parent(args.parent, target, _validate_date(args.start), "接入" if args.entry_only else "新建"))
    print(target)
    for note in notes:
        print(f"- {note}")


if __name__ == "__main__":
    main()
