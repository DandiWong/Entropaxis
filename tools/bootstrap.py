#!/usr/bin/env python3
"""
工作区根入口与系统配置初始化工具 (Bootstrap)
用于一键恢复工作区根目录的 AGENTS.md 与 CLAUDE.md 软链接。
"""
import os
import sys
from pathlib import Path

def setup_symlinks(verbose: bool = True) -> bool:
    tools_dir = Path(__file__).resolve().parent
    system_dir = tools_dir.parent
    ws_root = system_dir.parent
    root_configs = system_dir / "root-configs"

    if not root_configs.exists():
        print(f"❌ 错误: 未找到配置源目录 {root_configs}", file=sys.stderr)
        return False

    success = True
    for filename in ["AGENTS.md", "CLAUDE.md"]:
        src = root_configs / filename
        dst = ws_root / filename

        if not src.exists():
            if verbose:
                print(f"⚠️ 跳过缺失文件: {src}")
            continue

        try:
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            # 建立相对路径软链接，便于整体移动工作区目录
            dst.symlink_to(Path(".system/root-configs") / filename)
            if verbose:
                print(f"✅ 链接就绪: {filename} -> .system/root-configs/{filename}")
        except Exception as e:
            print(f"❌ 建立软链接失败 ({filename}): {e}", file=sys.stderr)
            success = False

    return success

if __name__ == "__main__":
    if setup_symlinks(verbose=True):
        print("\n✨ 工作区系统入口软链接初始化完成。")
    else:
        sys.exit(1)
