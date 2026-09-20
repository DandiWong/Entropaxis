#!/usr/bin/env python3
"""按 .entropaxis/schemas/ 声明校验结构化契约 (Schema Validator).

只支持 JSON Schema 的一个子集：type / required / properties / items / enum /
pattern / minLength / minItems / minimum / additionalProperties / conditional_required。
够用即可——引入完整 JSON Schema 库会违反纯标准库铁律。

schema 用 .json 而非 .yaml：Python 标准库没有 YAML 解析器，用 YAML 就得引入 pyyaml。

执行方式:
  python3 .entropaxis/tools/validate_schema.py                 # 校验全部已接线的目标
  python3 .entropaxis/tools/validate_schema.py --list          # 列出可用 schema
  python3 .entropaxis/tools/validate_schema.py <file.md>       # 校验单个 Markdown 的 Front Matter
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

try:
    from . import paths
except ImportError:
    import paths

HERE = Path(__file__).resolve().parent
SYSTEM_ROOT = paths.SYSTEM_DIR
WORKSPACE_ROOT = paths.WORKSPACE_ROOT
SCHEMA_DIR = SYSTEM_ROOT / "schemas"

FRONT_MATTER_RE = re.compile(r"^[\s\u00ad\u200b-\u200f\u202a-\u202e\u2060\ufeff]*---\n(.*?)\n---", re.DOTALL)
_TYPES = {
    "object": dict, "array": list, "string": str,
    "integer": int, "number": (int, float), "boolean": bool, "null": type(None),
}


def load_schema(name: str) -> dict:
    path = SCHEMA_DIR / f"{name}.schema.json"
    if not path.exists():
        raise FileNotFoundError(f"❌ 找不到 schema: {path}\n👉 用 --list 查看可用 schema 名。")
    return _resolve_refs(json.loads(path.read_text(encoding="utf-8")))


_REF_RE = re.compile(r"^(?P<file>[A-Za-z0-9_-]+)\.schema\.json#/(?P<pointer>.+)$")


def _resolve_refs(node, _depth: int = 0):
    """就地展开跨文件 `$ref`，形如 `front_matter.schema.json#/properties/carrier`。

    只支持同目录 + 属性路径这一种形态，不引入完整 JSON Pointer 与远程解析——够用即可。
    存在的理由：同一契约此前只能在两份 schema 里各抄一遍（承载三元组 ⇄ Audit 的
    reviewer_*），靠一条测试钉住防漂移；能引用就不该复制（元规则 #1）。
    深度上限兜住 A→B→A 的相互引用。
    """
    if _depth > 8:
        raise ValueError("$ref 展开超过 8 层，疑似循环引用")
    if isinstance(node, list):
        return [_resolve_refs(x, _depth + 1) for x in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        m = _REF_RE.match(ref)
        if not m:
            raise ValueError(f"不支持的 $ref 形态: {ref!r}（只支持 <name>.schema.json#/properties/<键>）")
        target = SCHEMA_DIR / f"{m['file']}.schema.json"
        if not target.exists():
            raise FileNotFoundError(f"$ref 指向不存在的 schema: {target}")
        cur = json.loads(target.read_text(encoding="utf-8"))
        for seg in m["pointer"].split("/"):
            if not isinstance(cur, dict) or seg not in cur:
                raise ValueError(f"$ref 指针无法解析: {ref!r}（断在 {seg!r}）")
            cur = cur[seg]
        merged = {k: v for k, v in node.items() if k != "$ref"}
        resolved = _resolve_refs(cur, _depth + 1)
        return {**resolved, **merged} if isinstance(resolved, dict) else resolved
    return {k: _resolve_refs(v, _depth + 1) for k, v in node.items()}


# `$` 前缀键的豁免只对 schema 文档（含 schema 自检样例）生效，且仅限标准
# JSON-Schema 指令键；数据实例（报告 Front Matter、audit-state
# sidecar）一律不豁免——第 12 轮外置复核先后实测 "$issues" 走私字段与
# "$comment" 携带状态文本穿过 additionalProperties:false。
_SCHEMA_DIRECTIVE_KEYS = frozenset(
    {"$schema", "$comment", "$id", "$ref", "$defs", "$anchor", "$dynamicRef"}
)


def validate(data, schema: dict, path: str = "$", *, allow_schema_directives: bool = False) -> list[str]:
    """返回违规说明列表；空列表表示通过。"""
    errs: list[str] = []
    expected = schema.get("type")
    if expected:
        alternatives = expected if isinstance(expected, list) else [expected]
        if not any(
            kind in _TYPES and isinstance(data, _TYPES[kind])
            and not (kind in ("integer", "number") and isinstance(data, bool))
            for kind in alternatives
        ):
            return [f"{path}: 期望 {expected}，实得 {type(data).__name__}"]

    # enum 对任意可比较类型生效（不限字符串）：此前只在 isinstance(data, str) 分支里
    # 检查，导致 {"type": "integer", "enum": [2]} 对整数值形同虚设——schema_version: 3
    # 这类非法整数会先通过上面的 type 检查（int 满足 type: integer），再因 enum 检查被
    # 跳过而彻底放行（第 6 轮外置复核实测抓到，R6-C2）。
    if "enum" in schema and data not in schema["enum"]:
        errs.append(f"{path}: 取值 {data!r} 不在允许集 {schema['enum']}")

    if isinstance(data, str):
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
                if key not in props and not (allow_schema_directives and key in _SCHEMA_DIRECTIVE_KEYS):
                    errs.append(f"{path}: 出现未声明字段 {key!r}（拼写错误或需先在 schema 中登记）")
        for key, sub in props.items():
            if key in data:
                errs += validate(data[key], sub, f"{path}.{key}", allow_schema_directives=allow_schema_directives)
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
                errs += validate(item, item_schema, f"{path}[{i}]", allow_schema_directives=allow_schema_directives)
    return errs


def parse_front_matter(text: str) -> dict | None:
    """解析 Markdown 顶部 Front Matter 的扁平 key: value；无 Front Matter 返回 None。"""
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return None
    out: dict = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # 分隔符取行内最先出现的 ASCII `:` 或全角 `：`：只认 ASCII 会让
        # `schema_version：2` 这类全角写法整行静默落空，键不存在即被上游
        # 判为旧契约（第 10 轮外置复核借此把只读校验降级到 legacy 放行）。
        cuts = [i for i in (line.find(":"), line.find("：")) if i != -1]
        if not cuts:
            continue
        cut = min(cuts)
        k, v = line[:cut], line[cut + 1:]
        v = v.strip()
        # 键规范化：剥 Cf 类格式字符（零宽/方向标记/BOM）再 NFKC 归一——
        # 第 12 轮终验实测 schema_version 键注入零宽字符后整行落空，路由被
        # 降级到 legacy 凭伪造 independence 放行。值不做规范化（保持原义）。
        k = unicodedata.normalize(
            "NFKC", "".join(ch for ch in k if unicodedata.category(ch) != "Cf")
        ).strip()
        out[k] = int(v) if v.isdigit() else v
    return out


def check_audit_report_schema_selftest() -> list[str]:
    """用 schema 自带的规范实例反向校验 schema 本身可用（防 schema 写坏而无人发现）。"""
    schema = load_schema("audit_report")
    instance = schema.get("instance")
    if not isinstance(instance, dict):
        return ["audit_report.schema.json 缺少 instance 自检样例"]
    return [f"audit_report 自检样例 {e}" for e in validate(instance, schema, allow_schema_directives=True)]


_URL_RE = re.compile(r"https?://[^\s)\]<>\u4e00-\u9fff]{6,}")
_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _table_headers(body: str) -> list[str]:
    """取 Markdown 表格的表头行（紧邻分隔行的上一行）。"""
    lines = body.splitlines()
    return [lines[i - 1] for i, ln in enumerate(lines)
            if i and _TABLE_SEP_RE.match(ln) and _TABLE_ROW_RE.match(lines[i - 1] + "\n")]


def check_deliverable_structure(path: Path, fm: dict, rule_key: str | None = None) -> list[str]:
    """校验交付物正文结构（产出合不合格），而非仅 Front Matter（文件在不在）。

    此前调度只判「文件写出来了」，《方案调研》的四段式、横向对比与信源标注全靠模型自觉——
    实测同一角色两次运行，一次带 6 条链接、一次 0 条，输出都算 succeeded。这里只挡结构性
    下限，不评判内容好坏：好坏归 Reviewer，本检查负责挡住「长得像但没做」。
    """
    schema = load_schema("deliverable_structure")
    # 阶段优先于 type：胶囊内 NN_类型.md 是既有命名铁律，02_方案 与 03_设计 同为
    # type: Proposal 却结构迥异，只按 type 取判据必然对其中一方误判。
    # 取判据的优先序：调用方显式指定 > 文件名阶段 > Front Matter type。
    # 显式指定这一层是必要的——Designer 的容器 README.md 既不叫 03_设计，type 又是
    # 与方案共用的 Proposal，只靠后两层必然套错判据。
    key = rule_key or next((v for pat, v in schema.get("stage_rules", {}).items() if re.match(pat, path.stem)), fm.get("type"))
    rules = schema["rules"].get(key)
    if not rules:
        return []
    body = FRONT_MATTER_RE.sub("", path.read_text(encoding="utf-8"), count=1)
    # 章节标签不只住在标题里：本工作区的报告惯用表头列（| 检查对象 | 证据 | 结论 |）
    # 与加粗短语（**总结论：阻断**）承载同样的结构角色，只扫标题会把合规报告判错。
    # 但只取表头行（分隔行之前那一行），数据行不算——否则对比表里一个「风险」单元格
    # 就能冒充「风险与 Plan B」章节，判据形同虚设。
    labels = "\n".join(_HEADING_RE.findall(body) + _BOLD_RE.findall(body) + _table_headers(body))
    missing = [pat for pat in rules["required_sections"]
               if not re.search(pat, labels, re.IGNORECASE)]
    errs: list[str] = []
    if missing:
        errs.append(f"缺少章节 {missing}；{rules['message']}")
    if rules.get("min_table_rows"):
        rows = [r for r in _TABLE_ROW_RE.findall(body) if not _TABLE_SEP_RE.match(r)]
        if len(rows) < rules["min_table_rows"]:
            errs.append(f"对比表数据行 {len(rows)} < 最少 {rules['min_table_rows']}；{rules['message']}")
    if rules.get("min_source_urls"):
        urls = set(_URL_RE.findall(body))
        if len(urls) < rules["min_source_urls"]:
            errs.append(f"带链接的信源 {len(urls)} 条 < 最少 {rules['min_source_urls']}；"
                        "无链接的引用无法复核，等同于未取证")
    return errs


def check_markdown(path: Path, structure: bool = True, rule_key: str | None = None) -> list[str]:
    """按 Front Matter 的 type 选择 schema：Audit 走 audit_report，其余走 front_matter。"""
    text = path.read_text(encoding="utf-8")
    fm = parse_front_matter(text)
    if fm is None:
        return [f"{path}: 未找到 Front Matter"]
    if fm.get("type") == "Audit":
        schema = load_schema("audit_report")["properties"]["front_matter"]
    else:
        schema = load_schema("front_matter")
    errs = [f"{path.name} {e}" for e in validate(fm, schema)]
    if structure:
        errs += [f"{path.name} 正文结构: {e}" for e in check_deliverable_structure(path, fm, rule_key)]
    return errs


def main() -> int:
    parser = argparse.ArgumentParser(description="按 .entropaxis/schemas/ 校验结构化契约")
    parser.add_argument("files", nargs="*", help="待校验的 Markdown 文件（省略则校验已接线目标）")
    parser.add_argument("--list", action="store_true", help="列出可用 schema")
    parser.add_argument("--no-structure", action="store_true", help="只校验 Front Matter，跳过正文结构判据")
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
            errs += check_markdown(p, structure=not args.no_structure)
    else:
        errs += check_audit_report_schema_selftest()

    if errs:
        for e in errs:
            print(f"❌ {e}", file=sys.stderr)
        print(f"\n👉 共 {len(errs)} 处违反 schema；按 .entropaxis/schemas/ 中的字段声明修正。", file=sys.stderr)
        return 1
    print("✅ 全部目标符合 schema 声明。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
