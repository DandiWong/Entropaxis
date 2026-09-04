#!/usr/bin/env python3
"""patent-combo 环境自检与降级矩阵生成器（纯标准库，零第三方依赖）。

用法:
  python3 scripts/check_env.py [--fix] [--json] [--skill-dir <path>] [--config <path>]

行为:
  1. 核心资产（内置三件套与最终交付收敛器）缺失 => 阻断（exit 2），不猜测替代路径。
  2. 增量依赖逐项探测: playwright 可导入、系统浏览器、prior-art CLI、verdict 后端。
  3. --fix: 仅尝试两类安全安装——pip 安装 playwright、cargo 安装 patent（均需对应
     工具链本机存在；任何安装失败不抛异常，转入降级矩阵）。
  4. 最终 DOCX 交付由内置收敛器调用随包 md_to_docx 工具；转换失败时保留底稿并阻断收尾，
     不得以 Markdown 替代。
  5. 每个失败项输出 ❌ 原因 + 👉 修复建议 + 降级方案；--json 输出结构化报告。

退出码: 0 = 核心阶段(Stage 0-3)可用; 2 = 核心资产缺失（阻断）。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

# ---------- 探针（可注入替换以便测试） ----------

def _importable(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False

def _which(binary: str) -> str | None:
    return shutil.which(binary)

def _detect_browser() -> str | None:
    """探测系统 Chrome/Edge；返回可读名称或 None。"""
    candidates: list[tuple[str, Path]] = []
    if sys.platform == "darwin":
        apps = Path("/Applications")
        candidates = [
            ("Google Chrome", apps / "Google Chrome.app"),
            ("Microsoft Edge", apps / "Microsoft Edge.app"),
            ("Chromium", apps / "Chromium.app"),
        ]
    elif sys.platform == "win32":
        pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        candidates = [
            ("Google Chrome", pf / "Google" / "Chrome" / "Application" / "chrome.exe"),
            ("Microsoft Edge", pf / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
        ]
    else:
        for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            if _which(name):
                return name
        return None
    for label, path in candidates:
        if path.exists():
            return label
    return None

def _default_config_path(skill_dir: Path) -> Path:
    workspace = skill_dir.parents[2]
    return workspace / ".data" / "patent_combo_config.json"

# ---------- 检查项 ----------

def check_core_assets(skill_dir: Path) -> list[dict]:
    """内置三件套存在性；缺失即阻断。"""
    required = {
        "disclosure_skill_md": "references/disclosure/skills/patent-disclosure/SKILL.md",
        "disclosure_prompts": "references/disclosure/skills/patent-disclosure/prompts",
        "md_to_docx": "references/disclosure/skills/patent-disclosure/tools/md_to_docx.py",
        "cnipa_crawl": "references/disclosure/skills/patent-disclosure/tools/crawl/cnipa_epub_search.py",
        "claims_guide": "references/claims-guide/PATENT_SKILL.md",
        "mining_rubric": "references/mining-rubric.md",
        "output_finalizer": "scripts/finalize_outputs.py",
    }
    checks = []
    for cid, rel in required.items():
        p = skill_dir / rel
        ok = p.exists()
        checks.append({
            "id": cid,
            "status": "ok" if ok else "missing",
            "detail": str(p),
            "fix": None if ok else "内置资产缺失：重新部署 patent-combo Skill 包，勿手工拼接",
            "degrade": None,
        })
    return checks

def check_output_root(config: dict, fix: bool) -> dict:
    raw = config.get("output_root", "")
    p = Path(raw).expanduser() if raw else None
    if not raw:
        return {"id": "output_root", "status": "missing",
                "detail": ".data/patent_combo_config.json 缺 output_root",
                "fix": "👉 在 .data/patent_combo_config.json 补 \"output_root\": \"<交底书草稿输出目录>\"",
                "degrade": "无输出根时不落盘，产物仅在会话中交付"}
    if p.exists():
        return {"id": "output_root", "status": "ok", "detail": str(p), "fix": None, "degrade": None}
    if fix:
        try:
            p.mkdir(parents=True, exist_ok=True)
            return {"id": "output_root", "status": "ok", "detail": f"{p}（已创建）", "fix": None, "degrade": None}
        except Exception as e:
            return {"id": "output_root", "status": "degraded", "detail": f"{p} 创建失败: {e}",
                    "fix": f"👉 手动创建目录: mkdir -p \"{p}\"",
                    "degrade": "产物仅在会话中交付，不落盘"}
    return {"id": "output_root", "status": "degraded", "detail": f"{p} 不存在",
            "fix": f"👉 mkdir -p \"{p}\" 或带 --fix 重跑",
            "degrade": "产物仅在会话中交付，不落盘"}

def check_word_export(importable_fn: Callable[[str], bool] = _importable) -> dict:
    required = {"docx": "python-docx", "latex2mathml": "latex2mathml", "yaml": "PyYAML"}
    missing = [package for module, package in required.items() if not importable_fn(module)]
    if not missing:
        return {"id": "word_export", "status": "ok", "detail": "DOCX 导出依赖可用", "fix": None, "degrade": None}
    packages = " ".join(missing)
    return {
        "id": "word_export",
        "status": "missing",
        "detail": f"DOCX 导出依赖缺失: {', '.join(missing)}",
        "fix": f"👉 pip install {packages} 后重跑环境自检",
        "degrade": "最终 DOCX 交付阻断；保留阶段 Markdown 底稿，不得以其替代交付",
    }

def check_playwright(fix: bool, importable_fn: Callable[[str], bool] = _importable,
                     installer: Callable[[list[str]], tuple[bool, str]] | None = None) -> dict:
    if importable_fn("playwright"):
        return {"id": "playwright", "status": "ok", "detail": "playwright 可导入", "fix": None, "degrade": None}
    if fix and installer:
        ok, out = installer([sys.executable, "-m", "pip", "install", "playwright>=1.40.0,<2.0"])
        if ok and importable_fn("playwright"):
            return {"id": "playwright", "status": "ok", "detail": "playwright 已安装", "fix": None, "degrade": None}
        detail = f"安装失败: {out.strip()[:200]}"
    else:
        detail = "playwright 未安装"
    return {"id": "playwright", "status": "missing", "detail": detail,
            "fix": "👉 pip install playwright（系统 Chrome/Edge 即可，无需 playwright install chromium）",
            "degrade": "Stage 4 CNIPA 路降级：04_查新报告该节标注「未执行（人工）」，改产关键词+IPC 分类号人工检索包（epub.cnipa.gov.cn）"}

def check_browser() -> dict:
    browser = _detect_browser()
    if browser:
        return {"id": "system_browser", "status": "ok", "detail": f"系统浏览器: {browser}", "fix": None, "degrade": None}
    return {"id": "system_browser", "status": "missing", "detail": "未找到系统 Chrome/Edge/Chromium",
            "fix": "👉 安装 Chrome/Edge，或 python -m playwright install chromium",
            "degrade": "Stage 4 CNIPA 路降级：同 playwright 缺失项（人工检索包）"}

def check_priorart_cli(cli_name: str, fix: bool,
                       which_fn: Callable[[str], str | None] = _which,
                       installer: Callable[[list[str]], tuple[bool, str]] | None = None) -> dict:
    path = which_fn(cli_name)
    if path:
        return {"id": "priorart_cli", "status": "ok", "detail": f"{cli_name} -> {path}", "fix": None, "degrade": None}
    if fix and installer and which_fn("cargo"):
        ok, out = installer(["cargo", "install", "patent", "--locked"])
        if ok and which_fn(cli_name):
            return {"id": "priorart_cli", "status": "ok", "detail": f"{cli_name} 已安装", "fix": None, "degrade": None}
        detail = f"cargo 安装失败: {out.strip()[:200]}"
    else:
        detail = f"未找到 CLI: {cli_name}"
    return {"id": "priorart_cli", "status": "missing", "detail": detail,
            "fix": "👉 安装 prior-art CLI（cargo install patent）；或修正 .data/patent_combo_config.json 的 priorart_cli 路径",
            "degrade": "Stage 4 dev-tool 路降级：04_查新报告该节标注「未执行」，改产一句话检索式清单供人工在对应源检索"}

def check_verdict_backend(which_fn: Callable[[str], str | None] = _which,
                          env: dict | None = None) -> dict:
    env = env if env is not None else os.environ
    if which_fn("ollama"):
        return {"id": "verdict_backend", "status": "ok", "detail": "本地 Ollama 可用（默认 qwen 系裁决）", "fix": None, "degrade": None}
    if env.get("PATENT_API_BASE") or env.get("OPENAI_API_KEY"):
        return {"id": "verdict_backend", "status": "ok", "detail": "OpenAI 兼容 API 已配置", "fix": None, "degrade": None}
    return {"id": "verdict_backend", "status": "degraded", "detail": "无 ollama 且未配置 PATENT_API_BASE / OPENAI_API_KEY",
            "fix": "👉 安装 Ollama 并拉取裁决模型，或 export PATENT_API_BASE/PATENT_API_KEY 指向 OpenAI 兼容服务",
            "degrade": "Stage 4 dev-tool 路自动以 --fast --keyword-only 降级为搜索级查新（无语义裁决，报告中明示）"}

def check_legacy_overrides(config: dict) -> dict:
    legacy = [k for k in ("disclosure_skill", "claims_guide") if config.get(k)]
    if legacy:
        return {"id": "legacy_overrides", "status": "ok",
                "detail": f"检测到旧版覆盖键 {legacy}，Stage 2/3 将改用外部路径（legacy 模式）",
                "fix": "👉 如需回归内置资产，从 .data/patent_combo_config.json 删除对应键", "degrade": None}
    return {"id": "legacy_overrides", "status": "ok", "detail": "无覆盖，使用内置资产", "fix": None, "degrade": None}

# ---------- 汇总 ----------

def build_report(skill_dir: Path, config: dict, fix: bool,
                 importable_fn: Callable[[str], bool] = _importable,
                 which_fn: Callable[[str], str | None] = _which,
                 env: dict | None = None,
                 installer: Callable[[list[str]], tuple[bool, str]] | None = None) -> dict:
    checks: list[dict] = []
    checks += check_core_assets(skill_dir)
    checks.append(check_output_root(config, fix))
    checks.append(check_playwright(fix, importable_fn, installer))
    checks.append(check_browser())
    checks.append(check_priorart_cli(str(config.get("priorart_cli") or "patent"), fix, which_fn, installer))
    checks.append(check_word_export(importable_fn))
    checks.append(check_verdict_backend(which_fn, env))
    checks.append(check_legacy_overrides(config))

    core_missing = [c for c in checks if c["id"].startswith(("disclosure", "md_to_docx", "cnipa", "claims", "mining", "output_finalizer", "word_export")) and c["status"] == "missing"]
    return {
        "version": "1.4.5",
        "core_ok": not core_missing,
        "ready_stages": ["Stage 0 脱敏门禁", "Stage 1 挖点", "Stage 2 交底书", "Stage 3 权利要求"] if not core_missing else [],
        "degraded_stages": (["Stage 4 CNIPA 路（人工检索包）"] if any(c["id"] in ("playwright", "system_browser") and c["status"] != "ok" for c in checks) else []) +
                           (["Stage 4 dev-tool 路（搜索级/未执行）"] if any(c["id"] in ("priorart_cli", "verdict_backend") and c["status"] != "ok" for c in checks) else []),
        "checks": checks,
    }

def render_text(report: dict) -> str:
    lines = ["🩺 patent-combo 环境自检", "=" * 32]
    for c in report["checks"]:
        mark = {"ok": "✅", "missing": "❌", "degraded": "⚠️ "}[c["status"]]
        lines.append(f"{mark} [{c['id']}] {c['detail']}")
        if c["status"] != "ok":
            if c.get("fix"):
                lines.append(f"   {c['fix']}")
            if c.get("degrade"):
                lines.append(f"   ↳ 降级: {c['degrade']}")
    lines.append("-" * 32)
    lines.append(f"核心阶段: {'✅ 就绪 ' + '、'.join(report['ready_stages']) if report['core_ok'] else '❌ 阻断（核心资产缺失）'}")
    if report["degraded_stages"]:
        lines.append(f"降级阶段: {'、'.join(report['degraded_stages'])}")
    return "\n".join(lines)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="patent-combo 环境自检与降级矩阵")
    parser.add_argument("--fix", action="store_true", help="尝试安全安装（pip playwright / cargo patent）")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    parser.add_argument("--skill-dir", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    skill_dir = Path(args.skill_dir).resolve()
    config_path = Path(args.config) if args.config else _default_config_path(skill_dir)
    config: dict = {}
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"❌ 实例配置解析失败: {config_path} ({e})", file=sys.stderr)
            print("👉 修正 .data/patent_combo_config.json 的 JSON 语法后重试", file=sys.stderr)
            return 2
    else:
        print(f"❌ 未找到实例配置: {config_path}", file=sys.stderr)
        print('👉 创建 .data/patent_combo_config.json，至少含 {"output_root": "<输出目录>"}', file=sys.stderr)
        return 2

    def installer(cmd: list[str]) -> tuple[bool, str]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            return r.returncode == 0, (r.stderr or r.stdout or "")
        except Exception as e:
            return False, str(e)

    report = build_report(skill_dir, config, args.fix, env=dict(os.environ), installer=installer)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0 if report["core_ok"] else 2

if __name__ == "__main__":
    sys.exit(main())
