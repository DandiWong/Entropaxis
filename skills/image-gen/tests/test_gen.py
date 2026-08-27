# -*- coding: utf-8 -*-
"""
image-gen 单元测试与基础逻辑校验
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

# 动态引入 scripts/gen.py
scripts_path = Path(__file__).resolve().parent.parent / "scripts" / "gen.py"
spec = importlib.util.spec_from_file_location("gen", scripts_path)
if spec is None or spec.loader is None:
    raise ImportError(f"Cannot load module from {scripts_path}")
gen_mod = importlib.util.module_from_spec(spec)
sys.modules["gen"] = gen_mod
spec.loader.exec_module(gen_mod)

format_instruction = gen_mod.format_instruction
get_monitored_image_dirs = gen_mod.get_monitored_image_dirs


def test_format_instruction():
    prompt = "A medical AI dashboard on #F2F2F2 background"
    instruction = format_instruction(prompt, size="16:9")
    assert "Use the imagegen tool" in instruction
    assert "16:9" in instruction
    assert prompt in instruction


def test_format_instruction_with_ref():
    prompt = "Restyle the diagram"
    instruction = format_instruction(
        prompt, size="2560x1440", ref_images=["ref.png"]
    )
    assert "reference image" in instruction
    assert "2560x1440" in instruction


def test_get_monitored_image_dirs():
    dirs = get_monitored_image_dirs()
    assert isinstance(dirs, list)
