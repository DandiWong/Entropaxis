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
    if not isinstance(before, dict) or not before or list(before)[-1] != "projects":
        return False
    try:
        new_text = append_yaml_items(text, "projects", [entry])
        after = yaml.safe_load(new_text) or {}
    except (yaml.YAMLError, ValueError):
        return False
    if (after.get("projects") or []) != [*(before.get("projects") or []), entry]:
        return False
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, p)
    return True


def append_yaml_items(text: str, key: str, additions: list, *, project_id: str | None = None) -> str:
    """只在指定列表追加；用 YAML 节点定位，保留其他字段和注释原文。"""
    if not additions:
        return text
    yaml = _yaml()
    if any(isinstance(token, (yaml.AnchorToken, yaml.AliasToken)) for token in yaml.scan(text)):
        raise ValueError("目标配置包含 YAML 锚点，请先展开目标列表再登记")
    node = yaml.compose(text)
    if not isinstance(node, yaml.MappingNode):
        raise ValueError("配置必须是 YAML 映射")
    if project_id is not None:
        projects = next((v for k, v in node.value if k.value == "projects"), None)
        if not isinstance(projects, yaml.SequenceNode):
            raise ValueError("projects 必须是列表")
        node = next((p for p in projects.value if isinstance(p, yaml.MappingNode)
                     and any(k.value == "id" and v.value == project_id for k, v in p.value)), None)
        if node is None:
            raise ValueError(f"项目不存在: {project_id}")
    pair = next(((k, v) for k, v in node.value if k.value == key), None)
    dumped = yaml.safe_dump(additions, allow_unicode=True, sort_keys=False)
    if pair:
        key_node, value = pair
        if isinstance(value, yaml.SequenceNode) and not value.flow_style:
            indent = value.start_mark.column
            pos = value.end_mark.index - value.end_mark.column
            if pos < value.start_mark.index:
                pos = value.end_mark.index
            block = "".join(" " * indent + line + "\n" for line in dumped.splitlines())
            prefix = "" if text[:pos].endswith("\n") else "\n"
            return text[:pos] + prefix + block + text[pos:]
        if isinstance(value, yaml.SequenceNode) and value.flow_style and value.value:
            # 非空行内列表只在闭括号前插入，不重排已有条目及其注释。
            fragment = text[value.start_mark.index:value.end_mark.index]
            tokens = list(yaml.scan(fragment))
            separator = " " if isinstance(tokens[-3], yaml.FlowEntryToken) else ", "
            extra = yaml.safe_dump(additions, allow_unicode=True, default_flow_style=True, width=100000).strip()[1:-1]
            pos = value.end_mark.index - 1
            return text[:pos] + separator + extra + text[pos:]
        if node.flow_style:
            raise ValueError("项目条目为行内映射，请先将该条目改为块格式再补字段")
        # 空值或空行内列表：只替换该值，保留同一行的人工注释。
        current = yaml.safe_load(text[value.start_mark.index:value.end_mark.index])
        if current is not None and not isinstance(current, list):
            raise ValueError(f"{key} 必须是列表")
        combined = (current or []) + additions
        indent = key_node.start_mark.column + 2
        block = "".join(" " * indent + line + "\n" for line in
                        yaml.safe_dump(combined, allow_unicode=True, sort_keys=False).splitlines())
        line_end = text.find("\n", value.end_mark.index)
        if line_end < 0:
            line_end = len(text)
        trailing = text[value.end_mark.index:line_end]
        # 行内注释留在键所在行；转为块列表，兼容后续 append_project。
        if trailing.strip() and not trailing.lstrip().startswith("#"):
            raise ValueError("不支持复杂行内 YAML，请将目标列表改为块格式")
        return text[:value.start_mark.index] + trailing + "\n" + block + text[min(line_end + 1, len(text)):]
    if node.flow_style:
        raise ValueError("项目条目为行内映射，请先将该条目改为块格式再补字段")
    indent = node.start_mark.column if project_id is not None else 0
    pos = node.end_mark.index - node.end_mark.column
    if pos < node.start_mark.index:
        pos = node.end_mark.index
    block = " " * indent + key + ":\n" + "".join(" " * (indent + 2) + line + "\n" for line in dumped.splitlines())
    prefix = "" if text[:pos].endswith("\n") else "\n"
    return text[:pos] + prefix + block + text[pos:]


def merge_scanned(root: Path, projects: list[dict], excludes: list[str],
                 shared_dirs: list[dict], updates: list[dict]) -> bool:
    """确认后增量登记；全部校验与暂存完成后替换，运行失败时还原已替换文件。"""
    try:
        from . import validate_schema
    except ImportError:
        import validate_schema
    yaml = _yaml()
    reg = registry_path(root)
    ws = reg.with_name("workspace-config.yaml")
    targets = [reg] + ([ws] if shared_dirs else [])
    originals = {p: p.read_bytes() for p in targets}
    texts = {p: b.decode("utf-8") for p, b in originals.items()}
    for p in targets:
        data = yaml.safe_load(texts[p])
        if not isinstance(data, dict):
            raise ValueError(f"配置必须是映射: {p.name}")
        policy = (data.get("_meta") or {}).get("policy", "merge-only")
        if policy not in ("merge-only", "append-only"):
            raise ValueError(f"不支持的写入策略: {policy}")
    data = yaml.safe_load(texts[reg])
    if updates and (data.get("_meta") or {}).get("policy") == "append-only":
        raise ValueError("append-only 注册表不能修改已登记项目的工程字段")
    existing = data.get("projects") or []
    by_id = {p["id"]: p for p in existing}
    by_path = {p["path"].rstrip("/"): p for p in existing}
    additions = []
    for p in projects:
        if p["path"].rstrip("/") in by_path:
            continue
        if p["id"] in by_id:
            raise ValueError(f"项目 ID 冲突: {p['id']}")
        additions.append(p)
        by_id[p["id"]] = p
        by_path[p["path"].rstrip("/")] = p
    texts[reg] = append_yaml_items(texts[reg], "projects", additions)
    existing_excludes = {p.rstrip("/") for p in data.get("exclude") or []}
    extra_excludes = []
    for p in excludes:
        if p.rstrip("/") not in existing_excludes:
            extra_excludes.append(p)
            existing_excludes.add(p.rstrip("/"))
    texts[reg] = append_yaml_items(texts[reg], "exclude", extra_excludes)
    for update in updates:
        existing_project = by_id.get(update["id"])
        if not existing_project or existing_project["path"].rstrip("/") != update["path"].rstrip("/"):
            raise ValueError("工程更新的所属项目不匹配")
        known = {p.rstrip("/") for p in existing_project.get("code") or []}
        extra = [p for p in update["code"] if p.rstrip("/") not in known]
        texts[reg] = append_yaml_items(texts[reg], "code", extra, project_id=update["id"])
    if shared_dirs:
        current = yaml.safe_load(texts[ws]).get("shared_dirs") or []
        known = {s["dir"].rstrip("/") for s in current}
        extra = []
        for item in shared_dirs:
            if item["dir"].rstrip("/") not in known:
                extra.append(item)
                known.add(item["dir"].rstrip("/"))
        texts[ws] = append_yaml_items(texts[ws], "shared_dirs", extra)
    for p in targets:
        errors = validate_schema.validate(yaml.safe_load(texts[p]), validate_schema.load_schema(p.stem))
        if errors:
            raise ValueError(f"{p.name}: " + "; ".join(errors))
    changed = [p for p in targets if texts[p].encode("utf-8") != originals[p]]
    if not changed:
        return True
    # 同一磁盘暂存；写入前再次核对，拒绝覆盖期间发生的人工修改。
    with tempfile.TemporaryDirectory(dir=reg.parent, prefix=".scan-") as tmp:
        staged = {}
        backups = {}
        for i, p in enumerate(changed):
            staged[p] = Path(tmp) / f"new-{i}"
            backups[p] = Path(tmp) / f"old-{i}"
            staged[p].write_text(texts[p], encoding="utf-8")
            backups[p].write_bytes(originals[p])
            os.chmod(staged[p], p.stat().st_mode & 0o777)
            os.chmod(backups[p], p.stat().st_mode & 0o777)
        for p in targets:
            if p.read_bytes() != originals[p]:
                raise ValueError(f"配置已变更，请重新扫描: {p.name}")
        committed = []
        try:
            for p in changed:
                os.replace(staged[p], p)
                committed.append(p)
        except OSError:
            for p in reversed(committed):
                os.replace(backups[p], p)
            raise
    return True
