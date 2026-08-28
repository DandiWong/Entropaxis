#!/usr/bin/env python3
"""
工作区根入口与系统配置初始化工具 (Bootstrap)
用于一键恢复工作区根目录的 AGENTS.md / CLAUDE.md 软链接，并自适应引导环境。
"""
import os
import sys
import shutil
from pathlib import Path

def setup_symlinks(verbose: bool = True) -> bool:
    tools_dir = Path(__file__).resolve().parent
    system_dir = tools_dir.parent
    ws_root = system_dir.parent
    root_configs = system_dir / "root-configs"

    if not root_configs.exists():
        if verbose:
            print(f"❌ 错误: 未找到配置源目录 {root_configs}", file=sys.stderr)
        return False

    success = True
    for filename in ["AGENTS.md", "CLAUDE.md"]:
        src = root_configs / filename
        dst = ws_root / filename

        if not src.exists():
            continue

        try:
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            # 建立相对路径软链接，保证跨机器迁移路径依然有效
            dst.symlink_to(Path(".system/root-configs") / filename)
            if verbose:
                print(f"✅ 入口链接就绪: {filename} -> .system/root-configs/{filename}")
        except Exception as e:
            if verbose:
                print(f"❌ 建立软链接失败 ({filename}): {e}", file=sys.stderr)
            success = False

    return success

def check_dashboard_token() -> bool:
    """检查看板凭证是否就绪"""
    config_path = Path.home() / ".config" / "internal-board" / "token"
    return config_path.exists() and len(config_path.read_text(encoding="utf-8").strip()) > 10

if __name__ == "__main__":
    print("🚀 开始初始化/自愈 Internal-Org 工作区配置...")
    if setup_symlinks(verbose=True):
        token_ready = check_dashboard_token()
        if token_ready:
            print("✅ 看板 API Token 已就绪。")
        else:
            print("ℹ️ 看板尚未配置 Token（可直接对 Agent 说「配置看板 Token」或让 Agent 执行 init.py）。")
        print("\n✨ 工作区初始化与自愈完成！")
    else:
        sys.exit(1)
