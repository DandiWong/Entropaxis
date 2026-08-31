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
            # 验证软链接是否真实建立（Windows 无权限时静默失败）
            if not dst.exists() and not dst.is_symlink():
                raise OSError("symlink created but not visible, falling back to hard link")
            if verbose:
                print(f"✅ 入口链接就绪: {filename} -> .system/root-configs/{filename}")
        except OSError:
            # 降级：NTFS 硬链接，同盘无需管理员权限
            try:
                os.link(src, dst)
                if verbose:
                    print(f"✅ 入口硬链接就绪（symlink 不可用）: {filename}")
            except Exception:
                # 最终降级：文件复制（需手动同步）
                try:
                    shutil.copy2(src, dst)
                    if verbose:
                        print(f"⚠️  入口文件复制就绪（需手动同步更新）: {filename}")
                except Exception as e3:
                    if verbose:
                        print(f"❌ 建立链接失败 ({filename}): {e3}", file=sys.stderr)
                    success = False

    return success

def check_dashboard_token() -> bool:
    """检查看板凭证是否就绪"""
    config_path = Path.home() / ".config" / "internal-board" / "token"
    return config_path.exists() and len(config_path.read_text(encoding="utf-8").strip()) > 10

def print_windows_hints() -> None:
    print("""
⚠️  Windows 环境已知限制（Agent 决策参考）：
  • 私有仓库：git clone 无法认证时，立即引导用户在已登录的浏览器中下载 ZIP，
    不要继续尝试其他认证方式。
  • 浏览器会话复用：需要接管浏览器登录态时，须先确认 Chrome 以
    --remote-debugging-port=9222 启动且端口已监听（netstat -an | findstr 9222），
    再行操作；否则直接放弃该路径。
  • PowerShell 输出异常：若 PowerShell 返回空或报错，改用 cmd /c 执行等效命令，
    或用 Out-File 将输出写入临时文件再读取。
  • 目录可见性：安装后需隐藏 .system 和 .data 目录：
    attrib +h +s <工作区路径>\\.system
    attrib +h +s <工作区路径>\\.data
""")

if __name__ == "__main__":
    print("🚀 开始初始化/自愈 Internal-Org 工作区配置...")
    if setup_symlinks(verbose=True):
        token_ready = check_dashboard_token()
        if token_ready:
            print("✅ 看板 API Token 已就绪。")
        else:
            print("ℹ️ 看板尚未配置 Token（可直接对 Agent 说「配置看板 Token」或让 Agent 执行 init.py）。")
        print("\n✨ 工作区初始化与自愈完成！")
        if sys.platform == "win32":
            print_windows_hints()
    else:
        sys.exit(1)
