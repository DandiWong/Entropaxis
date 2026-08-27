#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
image-gen: 调用本地 Codex CLI 原生能力生成/编辑图片的统一脚本
零第三方硬依赖（纯标准库），自动适配多种 CODEX_HOME 与 Orca 账户路径。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def get_monitored_image_dirs() -> list[Path]:
    """搜寻系统中所有可能存放 Codex 生成图片的目录"""
    dirs: list[Path] = []

    # 1. CODEX_HOME 环境变量
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        p = Path(env_home).expanduser() / "generated_images"
        if p.exists():
            dirs.append(p)

    # 2. 默认 ~/.codex/generated_images
    default_home = Path("~/.codex/generated_images").expanduser()
    if default_home.exists():
        dirs.append(default_home)

    # 3. Orca 管理的 Codex 账户目录
    orca_base = Path(
        "~/Library/Application Support/orca/codex-accounts"
    ).expanduser()
    if orca_base.exists():
        for acc in orca_base.glob("*/home/generated_images"):
            if acc.is_dir():
                dirs.append(acc)

    return list(dict.fromkeys(dirs))


def snapshot_images(dirs: list[Path]) -> set[Path]:
    """获取所有监控目录中的现有图片文件集合"""
    files: set[Path] = set()
    for d in dirs:
        try:
            for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
                files.update(d.glob(f"**/{ext}"))
        except OSError:
            pass
    return files


def check_codex_cli() -> str:
    """检查 codex CLI 是否存在且可用"""
    cli = shutil.which("codex")
    if not cli:
        print(
            "[FAIL] 未找到 codex CLI，请先安装 Codex 并运行 'codex login' 登录 ChatGPT 订阅账户。",
            file=sys.stderr,
        )
        sys.exit(3)
    return cli


def format_instruction(
    prompt: str,
    size: str | None = None,
    ref_images: list[str] | None = None,
) -> str:
    """格式化发送给 Codex 的提示词指令"""
    instruction = (
        "Use the imagegen tool to generate the image for the following request.\n"
        "Requirements: generate the image directly using the built-in image_gen tool, "
        "return only the visual result, no extra explanation.\n\n"
    )
    if size:
        instruction += f"Target Dimensions/Aspect Ratio: {size}\n\n"

    if ref_images:
        instruction += "Use the attached reference image(s) for style/composition/edit target.\n\n"

    instruction += f"Request:\n{prompt.strip()}\n"
    return instruction


def generate_image(
    prompt: str,
    out_path: Path,
    size: str | None = None,
    ref_images: list[str] | None = None,
    timeout: int = 240,
    verbose: bool = False,
) -> Path:
    """执行 Codex CLI 生图流程并保存结果"""
    _ = check_codex_cli()

    if ref_images:
        for ref in ref_images:
            if not Path(ref).exists():
                print(f"[FAIL] 参考图片不存在: {ref}", file=sys.stderr)
                sys.exit(4)

    # 1. 快照已有图片
    monitored_dirs = get_monitored_image_dirs()
    if not monitored_dirs:
        def_dir = Path("~/.codex/generated_images").expanduser()
        def_dir.mkdir(parents=True, exist_ok=True)
        monitored_dirs = [def_dir]

    before_images = snapshot_images(monitored_dirs)

    # 2. 组装 codex exec 指令
    cmd = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--enable",
        "image_generation",
    ]

    if ref_images:
        for ref in ref_images:
            cmd.extend(["-i", str(Path(ref).resolve())])

    instruction = format_instruction(prompt, size=size, ref_images=ref_images)

    if verbose:
        print(f"[*] 监控目录数量: {len(monitored_dirs)}")
        print(f"[*] 启动 codex exec 生图 (timeout={timeout}s)...")

    start_time = time.time()
    proc: subprocess.Popen[str] | None = None
    stdout = ""
    stderr = ""

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate(input=instruction, timeout=timeout)
    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
        print(f"[FAIL] codex exec 运行超时 ({timeout}s)。", file=sys.stderr)
        sys.exit(1)

    elapsed = time.time() - start_time
    if verbose and proc is not None:
        print(f"[*] codex exec 进程结束，耗时 {elapsed:.1f}s (rc={proc.returncode})")

    # 3. 重新扫描监控目录并比对差异
    after_dirs = get_monitored_image_dirs()
    after_images = snapshot_images(after_dirs)
    new_images: list[Path] = list(after_images - before_images)

    # 兜底：若集合差集为空，按最近修改时间查找最近 5 分钟内的新文件
    if not new_images:
        recent: list[Path] = []
        for d in after_dirs:
            for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
                for f in d.glob(f"**/{ext}"):
                    try:
                        if (time.time() - f.stat().st_mtime) < max(
                            elapsed + 30, 180
                        ):
                            recent.append(f)
                    except OSError:
                        pass
        if recent:
            recent.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            new_images = [recent[0]]

    if not new_images:
        err_out = (
            f"[FAIL] 未能从监控目录获取生成的图片文件。\n"
            f"进程输出尾部: {stdout[-300:] if stdout else ''}\n"
            f"错误日志尾部: {stderr[-300:] if stderr else ''}"
        )
        print(err_out, file=sys.stderr)
        sys.exit(1)

    # 选取最新生成的一张
    new_images.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    generated_file: Path = new_images[0]

    # 4. 拷贝到指定输出路径
    resolved_out = Path(out_path).resolve()
    resolved_out.parent.mkdir(parents=True, exist_ok=True)
    _ = shutil.copy(generated_file, resolved_out)

    file_size_kb = resolved_out.stat().st_size / 1024
    print(f"[OK] 生图成功: {resolved_out} ({file_size_kb:.1f} KB, 耗时 {elapsed:.1f}s)")
    return resolved_out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="通过本地 Codex CLI 调用 ChatGPT 订阅原生 imagegen 能力生成图片"
    )
    _ = parser.add_argument(
        "--prompt", "-p", required=True, help="生图提示词 (Prompt)"
    )
    _ = parser.add_argument(
        "--out", "-o", required=True, help="输出文件路径 (例如 output/slide.png)"
    )
    _ = parser.add_argument(
        "--size",
        "-s",
        default="16:9",
        help="尺寸或画幅比例 (例如 2560x1440, 1024x1024, 16:9, 4:3 等)",
    )
    _ = parser.add_argument(
        "--ref",
        "-r",
        action="append",
        default=[],
        help="参考图片路径 (可多次传入用于多图参考或 i2i 编辑)",
    )
    _ = parser.add_argument(
        "--timeout",
        "-t",
        type=int,
        default=240,
        help="超时时间 (秒，默认 240)",
    )
    _ = parser.add_argument(
        "--verbose", "-v", action="store_true", help="输出详细调试信息"
    )

    args = parser.parse_args()
    _ = generate_image(
        prompt=str(args.prompt),
        out_path=Path(str(args.out)),
        size=str(args.size) if args.size else None,
        ref_images=list(args.ref) if args.ref else None,
        timeout=int(args.timeout),
        verbose=bool(args.verbose),
    )


if __name__ == "__main__":
    main()
