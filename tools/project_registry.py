"""工作区项目注册表的唯一读写入口（data/templates/registry.yaml，契约 schemas/registry.schema.json）。

读取方（resolve_project / find_capsule / lint_workspace）与写入方（init_project）都经这里，
不再各自解析——此前三处各写一份 Markdown 表格正则，列一漂移就静默读错。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

try:
    from . import paths
except ImportError:
    import paths

REGISTRY_NAME = "registry.yaml"


def _yaml():
    try:
        import yaml  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("❌ 读取项目注册表需要 PyYAML\n👉 pip install pyyaml") from exc
    return yaml


def registry_path(root: Path) -> Path:
    return root / paths.SYSTEM_DIRNAME / "data" / "templates" / REGISTRY_NAME


def _raw(root: Path) -> dict:
    p = registry_path(root)
    if not p.is_file():
        return {}
    data = _yaml().safe_load(p.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def load_projects(root: Path) -> list[dict]:
    """返回规范化项目列表；注册表缺失是合法初始态，返回空表。

    `path` 去掉尾部斜杠；`parent` 缺省为空串（顶层）；层级只取 parent，不从路径推断。
    """
    projects = []
    for p in _raw(root).get("projects") or []:
        if not isinstance(p, dict) or not p.get("id") or not p.get("path"):
            continue
        projects.append({
            "id": str(p["id"]),
            "name": str(p.get("name") or p["id"]),
            "path": str(p["path"]).rstrip("/"),
            "parent": str(p.get("parent") or ""),
            "aliases": [str(a) for a in p.get("aliases") or []],
            "boards": dict(p.get("boards") or {}),
            "code": [str(c).rstrip("/") for c in p.get("code") or []],
            "note": str(p.get("note") or ""),
        })
    return projects


def load_excludes(root: Path) -> list[str]:
    return [str(e) for e in _raw(root).get("exclude") or []]


def append_project(root: Path, entry: dict) -> bool:
    """在 `projects:` 列表末尾追加一项，保留文件其余文本与注释（merge-only）。

    按文本追加而非整文件重写，避免丢失人工注释；追加后重新解析校验，
    结果不是"原列表 + 新项"（例如 projects 不在文件末尾）就放弃写入。
    """
    p = registry_path(root)
    if not p.is_file():
        return False
    yaml = _yaml()
    text = p.read_text(encoding="utf-8")
    before = yaml.safe_load(text) or {}
    item = yaml.safe_dump([entry], allow_unicode=True, sort_keys=False, default_flow_style=None)
    new_text = text.rstrip("\n") + "\n" + "".join(f"  {ln}\n" for ln in item.splitlines())
    try:
        after = yaml.safe_load(new_text) or {}
    except yaml.YAMLError:
        return False
    if (after.get("projects") or []) != [*(before.get("projects") or []), entry]:
        return False
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, p)
    return True
