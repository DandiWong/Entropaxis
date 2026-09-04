#!/usr/bin/env python3
"""按 .system/schemas/ 声明校验结构化契约 (Schema Validator).

只支持 JSON Schema 的一个子集：type / required / properties / items / enum /
pattern / minLength / minItems / minimum / additionalProperties / conditional_required。
够用即可——引入完整 JSON Schema 库会违反纯标准库铁律。

schema 用 .json 而非 .yaml：Python 标准库没有 YAML 解析器，用 YAML 就得引入 pyyaml。

执行方式:
  python3 .system/tools/validate_schema.py                 # 校验全部已接线的目标
  python3 .system/tools/validate_schema.py --list          # 列出可用 schema
  python3 .system/tools/validate_schema.py <file.md>       # 校验单个 Markdown 的 Front Matter
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYSTEM_ROOT = HERE.parent
WORKSPACE_ROOT = SYSTEM_ROOT.parent
SCHEMA_DIR = SYSTEM_ROOT / "schemas"

FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_TYPES = {
    "object": dict, "array": list, "string": str,
    "integer": int, "number": (int, float), "boolean": bool,
}


def load_schema(name: str) -> dict:
    path = SCHEMA_DIR / f"{name}.schema.json"
    if not path.exists():
        raise FileNotFoundError(f"❌ 找不到 schema: {path}\n👉 用 --list 查看可用 schema 名。")
    return json.loads(path.read_text(encoding="utf-8"))


def validate(data, schema: dict, path: str = "$") -> list[str]:
    """返回违规说明列表；空列表表示通过。"""
    errs: list[str] = []
    expected = schema.get("type")
    if expected:
        py = _TYPES.get(expected)
        # bool 是 int 的子类，整数校验必须显式排除，否则 true 会被当成合法整数
        if py and (not isinstance(data, py) or (expected in ("integer", "number") and isinstance(data, bool))):
            return [f"{path}: 期望 {expected}，实得 {type(data).__name__}"]

    if isinstance(data, str):
        if "enum" in schema and data not in schema["enum"]:
            errs.append(f"{path}: 取值 {data!r} 不在允许集 {schema['enum']}")
        if "pattern" in schema and not re.search(schema["pattern"], data):
            errs.append(f"{path}: 取值 {data!r} 不匹配 /{schema['pattern']}/")
        if "minLength" in schema and len(data) < schema["minLength"]:
            errs.append(f"{path}: 长度 {len(data)} < 最小 {schema['minLength']}")

    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            errs.append(f"{path}: 取值 {data} < 最小 {schema['minimum']}")

    if isinstance(data, dict):
        for key in schema.get("required", []):
            if key not in data:
                errs.append(f"{path}: 缺少必填字段 {key!r}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in data:
                if key not in props and not key.startswith("$"):
                    errs.append(f"{path}: 出现未声明字段 {key!r}（拼写错误或需先在 schema 中登记）")
        for key, sub in props.items():
            if key in data:
                errs += validate(data[key], sub, f"{path}.{key}")
        for cond in schema.get("conditional_required", []):
            if all(data.get(k) == v for k, v in cond.get("when", {}).items()):
                for key in cond.get("require", []):
                    if key not in data:
                        errs.append(f"{path}: {cond.get('message', f'条件必填字段 {key} 缺失')}")

    if isinstance(data, list):
        if "minItems" in schema and len(data) < schema["minItems"]:
            errs.append(f"{path}: 元素数 {len(data)} < 最小 {schema['minItems']}")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(data):
                errs += validate(item, item_schema, f"{path}[{i}]")
    return errs


def parse_front_matter(text: str) -> dict | None:
    """解析 Markdown 顶部 Front Matter 的扁平 key: value；无 Front Matter 返回 None。"""
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return None
    out: dict = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        out[k.strip()] = int(v) if v.isdigit() else v
    return out


def check_route_map() -> list[str]:
    target = WORKSPACE_ROOT / ".system" / "rules" / "route_map.json"
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"route_map.json 解析失败: {exc}"]
    return [f"route_map.json {e}" for e in validate(data, load_schema("route_map"))]


def check_audit_report_schema_selftest() -> list[str]:
    """用 schema 自带的规范实例反向校验 schema 本身可用（防 schema 写坏而无人发现）。"""
    schema = load_schema("audit_report")
    instance = schema.get("instance")
    if not isinstance(instance, dict):
        return ["audit_report.schema.json 缺少 instance 自检样例"]
    return [f"audit_report 自检样例 {e}" for e in validate(instance, schema)]


def check_markdown(path: Path) -> list[str]:
    """按 Front Matter 的 type 选择 schema：Audit 走 audit_report，其余走 front_matter。"""
    fm = parse_front_matter(path.read_text(encoding="utf-8"))
    if fm is None:
        return [f"{path}: 未找到 Front Matter"]
    if fm.get("type") == "Audit":
        schema = load_schema("audit_report")["properties"]["front_matter"]
    else:
        schema = load_schema("front_matter")
    return [f"{path.name} {e}" for e in validate(fm, schema)]


def main() -> int:
    parser = argparse.ArgumentParser(description="按 .system/schemas/ 校验结构化契约")
    parser.add_argument("files", nargs="*", help="待校验的 Markdown 文件（省略则校验已接线目标）")
    parser.add_argument("--list", action="store_true", help="列出可用 schema")
    args = parser.parse_args()

    if args.list:
        for p in sorted(SCHEMA_DIR.glob("*.schema.json")):
            s = json.loads(p.read_text(encoding="utf-8"))
            print(f"  {p.stem.replace('.schema',''):16} → {s.get('target', '?')}")
        return 0

    errs: list[str] = []
    if args.files:
        for f in args.files:
            p = Path(f)
            if not p.exists():
                print(f"❌ 文件不存在: {p}\n👉 检查路径拼写。", file=sys.stderr)
                return 2
            errs += check_markdown(p)
    else:
        errs += check_route_map()
        errs += check_audit_report_schema_selftest()

    if errs:
        for e in errs:
            print(f"❌ {e}", file=sys.stderr)
        print(f"\n👉 共 {len(errs)} 处违反 schema；按 .system/schemas/ 中的字段声明修正。", file=sys.stderr)
        return 1
    print("✅ 全部目标符合 schema 声明。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
