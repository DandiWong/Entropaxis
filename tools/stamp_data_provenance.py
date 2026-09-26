#!/usr/bin/env python3
"""为 data/ 顶层实例文件加盖来源与写入策略标记 (Data Provenance Stamper).

解决两个问题：
  1. `data/` 目录人眼看不出每个文件从哪来、谁在维护、能不能重写；
  2. 缺少「已存在即不得整体重写」的显式声明，导致人工配置被 Agent 覆盖。

Markdown 加 YAML Front Matter，JSON 加 `_meta` 键。已有标记的文件只补缺失字段，
不覆盖既有取值——本工具自身必须遵守它所声明的 merge-only 策略。

执行方式:
  python3 .entropaxis/tools/stamp_data_provenance.py            # 补盖缺失标记
  python3 .entropaxis/tools/stamp_data_provenance.py --check    # 只报告不写入，缺标记时退出码 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from . import paths
except ImportError:
    import paths

WORKSPACE_ROOT = paths.WORKSPACE_ROOT
DATA_DIR = paths.DATA_DIR

VALID_POLICIES = ("merge-only", "append-only", "regenerate-safe")

# 已知实例文件的来源声明。未登记的文件按 human/merge-only 兜底——
# 默认最保守：不确定来源时一律当人工真源保护，宁可少改不可误删。
KNOWN: dict[str, dict[str, str]] = {
    "file-opener.json": {
        "source": ".entropaxis/templates/instance/file-opener.template.json",
        "managed_by": "bootstrap.py init_file_opener",
        "policy": "merge-only",
        "note": "标 arbitrated 的条目为人工仲裁，任何写入方不得覆盖；全量重扫须显式 --force-rescan-opener",
    },
    "workspace-config.yaml": {
        "source": ".entropaxis/templates/instance/workspace-config.template.yaml",
        "managed_by": "bootstrap.py render_instance_configs（仅缺失时渲染）+ 人工填写",
        "policy": "merge-only",
    },
    "roles.yaml": {
        "source": ".entropaxis/templates/instance/roles.template.yaml",
        "managed_by": "bootstrap.py render_instance_configs（仅缺失时渲染）+ setup_agents.py --set-role + 人工",
        "policy": "merge-only",
    },
    "registry.md": {
        "source": ".entropaxis/templates/instance/registry.template.md",
        "managed_by": "init_project.py 立项时追加映射行 + 人工维护排除规则与备注",
        "policy": "merge-only",
    },
    "tips.md": {
        "source": "人工创建",
        "managed_by": "Agent 按《工作流指令》生成或更新工作空间 Tips（须用户确认）",
        "policy": "append-only",
    },
    "规则案例库.md": {
        "source": "人工创建",
        "managed_by": "Agent 按《工作流指令》复盘的案例收录条款",
        "policy": "append-only",
    },
}

FALLBACK = {"source": "未登记（按人工真源保护）", "managed_by": "人工", "policy": "merge-only"}


# 两个来源桶：路径本身即指向定义方（见 rules/控制面布局.md「data/ 目录结构」）；Skill 配置随 Skill 自身目录
SOURCE_BUCKETS = ("templates", "rules")


def target_files(data_dir: Path = DATA_DIR) -> list[Path]:
    """只取两个来源桶内的 .md/.json/.yaml；credentials/ 与 docs/ 一律不碰（前者含凭据）。"""
    if not data_dir.is_dir():
        return []
    return sorted(
        p for b in SOURCE_BUCKETS for p in (data_dir / b).rglob("*")
        if p.is_file() and p.suffix in DATA_SUFFIXES
    )


DATA_SUFFIXES = (".md", ".json", ".yaml")


def yaml_has_meta(text: str) -> bool:
    """YAML 实例以顶层 `_meta:` 映射承载标记（与 JSON 同构），须含 policy。"""
    lines = text.splitlines()
    if "_meta:" not in lines:
        return False
    for line in lines[lines.index("_meta:") + 1:]:
        if not line.startswith((" ", "\t")):
            return False
        if line.strip().startswith("policy:"):
            return True
    return False


def _meta_for(name: str) -> dict[str, str]:
    return dict(KNOWN.get(name, FALLBACK))


def has_provenance(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if path.suffix == ".json":
        try:
            return isinstance(json.loads(text).get("_meta"), dict)
        except (json.JSONDecodeError, AttributeError):
            return False
    if path.suffix == ".yaml":
        return yaml_has_meta(text)
    return text.startswith("---\n") and "\npolicy:" in text.split("\n---", 2)[0]


def stamp(path: Path) -> bool:
    """补盖缺失标记；已有标记则不动。返回是否发生写入。"""
    if has_provenance(path):
        return False
    meta = _meta_for(path.name)
    text = path.read_text(encoding="utf-8")

    if path.suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return False
        if not isinstance(data, dict):
            return False
        # _meta 置于首位，便于人眼一打开就看到来源
        data = {"_meta": meta, **data}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return True

    if path.suffix == ".yaml":
        # 文本前插而非解析重写：保留人工注释与键序
        block = "_meta:\n" + "".join(f"  {k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in meta.items())
        path.write_text(block + text.lstrip("\n"), encoding="utf-8")
        return True

    lines = [f"{k}: {v}" for k, v in meta.items()]
    path.write_text("---\n" + "\n".join(lines) + "\n---\n\n" + text.lstrip("\n"), encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="为 .entropaxis/data/ 顶层实例文件加盖来源与写入策略标记")
    parser.add_argument("--check", action="store_true", help="只报告不写入；存在缺标记文件时退出码 1")
    args = parser.parse_args()

    files = target_files()
    if not files:
        print("ℹ️ .entropaxis/data/ 下没有需要加盖标记的顶层 .md/.json 文件。")
        return 0

    missing = [p for p in files if not has_provenance(p)]
    if args.check:
        for p in missing:
            print(f"⚠️ 缺少来源标记: .entropaxis/data/{p.relative_to(DATA_DIR)}\n👉 运行 python3 .entropaxis/tools/stamp_data_provenance.py 补盖")
        print(f"{len(files) - len(missing)}/{len(files)} 个文件已有来源标记。")
        return 1 if missing else 0

    for p in missing:
        if stamp(p):
            meta = _meta_for(p.name)
            print(f"✅ .entropaxis/data/{p.relative_to(DATA_DIR)} ← 来源 {meta['source']}｜策略 {meta['policy']}")
    if not missing:
        print(f"✅ .entropaxis/data/ 全部 {len(files)} 个实例文件均已有来源标记。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
