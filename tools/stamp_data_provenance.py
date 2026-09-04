#!/usr/bin/env python3
"""为 .data/ 顶层实例文件加盖来源与写入策略标记 (Data Provenance Stamper).

解决两个问题：
  1. `.data/` 目录人眼看不出每个文件从哪来、谁在维护、能不能重写；
  2. 缺少「已存在即不得整体重写」的显式声明，导致人工配置被 Agent 覆盖。

Markdown 加 YAML Front Matter，JSON 加 `_meta` 键。已有标记的文件只补缺失字段，
不覆盖既有取值——本工具自身必须遵守它所声明的 merge-only 策略。

执行方式:
  python3 .system/tools/stamp_data_provenance.py            # 补盖缺失标记
  python3 .system/tools/stamp_data_provenance.py --check    # 只报告不写入，缺标记时退出码 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE_ROOT = HERE.parent.parent
DATA_DIR = WORKSPACE_ROOT / ".data"

VALID_POLICIES = ("merge-only", "append-only", "regenerate-safe")

# 已知实例文件的来源声明。未登记的文件按 human/merge-only 兜底——
# 默认最保守：不确定来源时一律当人工真源保护，宁可少改不可误删。
KNOWN: dict[str, dict[str, str]] = {
    "file-opener.json": {
        "source": ".system/templates/file-opener.template.json",
        "managed_by": "bootstrap.py init_file_opener",
        "policy": "merge-only",
        "note": "标 arbitrated 的条目为人工仲裁，任何写入方不得覆盖；全量重扫须显式 --force-rescan-opener",
    },
    "board_config.json": {
        "source": ".system/templates/board_config.template.json",
        "managed_by": "bootstrap.py render_data_templates（仅缺失时渲染）",
        "policy": "merge-only",
    },
    "workspace-config.md": {
        "source": ".system/templates/workspace-config.template.md",
        "managed_by": "bootstrap.py render_data_templates（仅缺失时渲染）+ 人工填写",
        "policy": "merge-only",
    },
    "registry.md": {
        "source": "人工创建",
        "managed_by": "人工 + Agent 按《项目组织》工作区项目索引维护",
        "policy": "merge-only",
    },
    "reimbursement-config.md": {
        "source": "人工创建",
        "managed_by": "人工 + Agent 按《财务报销》配置前置门禁补填",
        "policy": "merge-only",
    },
    "config.json": {
        "source": ".system/skills/patent-combo/",
        "managed_by": "patent-combo Skill",
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


# 三个来源桶：路径本身即指向定义方（见 rules/01_根系统治理.md「.data/ 目录结构」）
SOURCE_BUCKETS = ("templates", "rules", "skills")


def target_files(data_dir: Path = DATA_DIR) -> list[Path]:
    """只取三个来源桶内的 .md/.json；credentials/ 与 docs/ 一律不碰（前者含凭据）。"""
    if not data_dir.is_dir():
        return []
    return sorted(
        p for b in SOURCE_BUCKETS for p in (data_dir / b).rglob("*")
        if p.is_file() and p.suffix in (".md", ".json")
    )


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

    lines = [f"{k}: {v}" for k, v in meta.items()]
    path.write_text("---\n" + "\n".join(lines) + "\n---\n\n" + text.lstrip("\n"), encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="为 .data/ 顶层实例文件加盖来源与写入策略标记")
    parser.add_argument("--check", action="store_true", help="只报告不写入；存在缺标记文件时退出码 1")
    args = parser.parse_args()

    files = target_files()
    if not files:
        print("ℹ️ .data/ 下没有需要加盖标记的顶层 .md/.json 文件。")
        return 0

    missing = [p for p in files if not has_provenance(p)]
    if args.check:
        for p in missing:
            print(f"⚠️ 缺少来源标记: .data/{p.relative_to(DATA_DIR)}\n👉 运行 python3 .system/tools/stamp_data_provenance.py 补盖")
        print(f"{len(files) - len(missing)}/{len(files)} 个文件已有来源标记。")
        return 1 if missing else 0

    for p in missing:
        if stamp(p):
            meta = _meta_for(p.name)
            print(f"✅ .data/{p.relative_to(DATA_DIR)} ← 来源 {meta['source']}｜策略 {meta['policy']}")
    if not missing:
        print(f"✅ .data/ 全部 {len(files)} 个实例文件均已有来源标记。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
