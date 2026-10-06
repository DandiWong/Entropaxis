#!/usr/bin/env python3
"""存量目录整理执行器：按 moves.tsv 无覆盖移动，BEGIN/DONE 日志可撤销。

用法（路径一律相对工作区根）：

    python3 .entropaxis/tools/apply_moves.py <moves.tsv>                 # dry-run，打印计划
    python3 .entropaxis/tools/apply_moves.py <moves.tsv> --apply
    python3 .entropaxis/tools/apply_moves.py <moves.tsv> --undo [--apply]

清单每行「源<TAB>目标」，空行与 # 注释行忽略；日志 `<清单>.log` 逐条 fsync，
状态取 BEGIN/DONE/UNDO_BEGIN/UNDONE，两个方向都是先记意图、后动文件。
退出码：0 成功 / 1 执行中冲突或异常（已完成部分保留）/ 2 预检拒绝（零改动）/ 3 待人工核对。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

try:
    from . import paths
except ImportError:
    import paths

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PRECHECK = 2
EXIT_MANUAL = 3

LOG_STATUSES = ("BEGIN", "DONE", "UNDO_BEGIN", "UNDONE")


def _read_manifest(manifest_path: Path) -> tuple[list[tuple[int, str, str]], list[str]]:
    """解析清单，返回 (移动行, 格式错误)；每项为 (行号, 源, 目标)。"""
    moves: list[tuple[int, str, str]] = []
    errors: list[str] = []
    for lineno, raw in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            errors.append(f"第 {lineno} 行不是「源<TAB>目标」两列：{stripped}")
            continue
        moves.append((lineno, parts[0].strip(), parts[1].strip()))
    return moves, errors


def _shared_dirs(root: Path) -> list[Path]:
    """workspace-config.yaml 的 shared_dirs 目录（缺失或无该键时视为没有共享层）。"""
    config = root / paths.SYSTEM_DIRNAME / "data" / "templates" / "workspace-config.yaml"
    if not config.is_file():
        return []
    shared: list[Path] = []
    in_section = False
    for raw in config.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw[0].isspace():
            in_section = stripped.split(":", 1)[0] == "shared_dirs"
            continue
        if not in_section or not stripped.startswith("-"):
            continue
        match = re.search(r"\bdir:\s*([^,}]+)", stripped)
        if match:
            shared.append(root / match.group(1).strip().strip("'\"").rstrip("/"))
    return shared


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists():
        if current.parent == current:
            return current
        current = current.parent
    return current


def _precheck(
    moves: list[tuple[int, str, str]],
    states: dict[int, set[str]],
    root: Path,
    undo: bool,
) -> list[str]:
    """整单预检：任一问题即拒绝、零移动。存在性只查无日志标记的未动行——
    已有 BEGIN/DONE 等标记的行属中断恢复射程，物理半完成态由恢复核对判定。"""
    problems: list[str] = []
    shared = _shared_dirs(root)
    sources = {src for _, src, _ in moves}
    seen_targets: dict[str, int] = {}
    for lineno, src, dst in moves:
        for label, value in (("源", src), ("目标", dst)):
            path = Path(value)
            if path.is_absolute():
                problems.append(f"第 {lineno} 行{label}是绝对路径：{value}")
                continue
            resolved = (root / value).resolve()
            if resolved != root and root not in resolved.parents:
                problems.append(f"第 {lineno} 行{label}越出工作区根：{value}")
                continue
            if any(resolved == d or d in resolved.parents for d in shared):
                problems.append(f"第 {lineno} 行{label}落在共享资料目录内（共享层由用户手工移动）：{value}")
        if dst in seen_targets:
            problems.append(f"第 {lineno} 行与第 {seen_targets[dst]} 行目标重复：{dst}")
        else:
            seen_targets[dst] = lineno
        if dst in sources:
            problems.append(f"第 {lineno} 行目标是其他行的源：{dst}")
        if undo or states.get(lineno):
            # 已有日志标记的行由中断恢复负责；撤销只处理已完成行，从未执行的行不在撤销射程，
            # 撤销时的占用冲突由无覆盖原语在执行时拒绝
            continue
        # 存在性按运动方向判定（撤销方向源、目标对调）
        source, target = (dst, src) if undo else (src, dst)
        source_path, target_path = root / source, root / target
        if not source_path.exists():
            problems.append(f"第 {lineno} 行源不存在：{source}")
        if target_path.exists() or target_path.is_symlink():
            problems.append(f"第 {lineno} 行目标已存在：{target}")
        try:
            if (
                _nearest_existing_parent(source_path).stat().st_dev
                != _nearest_existing_parent(target_path).stat().st_dev
            ):
                problems.append(f"第 {lineno} 行源与目标最近已存在父目录不在同一设备：{src} → {dst}")
        except OSError as exc:
            problems.append(f"第 {lineno} 行无法判定设备归属：{exc}")
    return problems


def _read_log(log_path: Path) -> dict[int, set[str]]:
    states: dict[int, set[str]] = {}
    if not log_path.is_file():
        return states
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        parts = raw.split("\t")
        if len(parts) != 4 or parts[0] not in LOG_STATUSES:
            continue
        try:
            lineno = int(parts[1])
        except ValueError:
            continue
        states.setdefault(lineno, set()).add(parts[0])
    return states


def _log(log_path: Path, status: str, lineno: int, src: str, dst: str) -> None:
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{status}\t{lineno}\t{src}\t{dst}\n")
        handle.flush()
        os.fsync(handle.fileno())


def _move_noreplace(src: Path, dst: Path) -> None:
    """无覆盖移动原语：目标已存在即抛错，绝不覆盖他人内容。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir() and not src.is_symlink():
        os.mkdir(dst)  # 独占占位，已存在即 FileExistsError
        try:
            os.rename(src, dst)  # POSIX 只允许替换空目录
        except OSError:
            try:
                os.rmdir(dst)  # 仅删空占位；非空说明他人写入，保留
            except OSError:
                pass
            raise
    else:
        os.link(src, dst, follow_symlinks=False)  # 已存在即 FileExistsError
        os.unlink(src)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _recover(
    moves: list[tuple[int, str, str]],
    states: dict[int, set[str]],
    log_path: Path,
    root: Path,
    undo: bool,
) -> int | None:
    """恢复核对：对有意图无完成的行按物理状态判定（撤销方向源、目标对调后同表）。

    返回 EXIT_MANUAL 表示无法自动判定（含目标位置为空目录），不做任何改动。
    """
    begin_marker, done_marker = ("UNDO_BEGIN", "UNDONE") if undo else ("BEGIN", "DONE")
    for lineno, src, dst in moves:
        marks = states.get(lineno, set())
        if begin_marker not in marks or done_marker in marks:
            continue
        source, target = (dst, src) if undo else (src, dst)
        source_path, target_path = root / source, root / target
        source_exists, target_exists = source_path.exists(), target_path.exists()
        if target_exists and not source_exists:
            _log(log_path, done_marker, lineno, src, dst)  # 已完成，补记完成标记
            marks.add(done_marker)
        elif source_exists and not target_exists:
            continue  # 未执行，按正常流程重做
        elif source_exists and target_exists and _same_file(source_path, target_path):
            try:
                os.unlink(source_path)  # 已 link 未 unlink：补 unlink
            except OSError:
                print(f"❌ 第 {lineno} 行无法补 unlink，转人工核对（不做任何改动）：{source_path}")
                return EXIT_MANUAL
            _log(log_path, done_marker, lineno, src, dst)
            marks.add(done_marker)
        else:
            # 含目标位置为空目录、两端内容不同、两端都不存在：空目录不推断为工具占位
            print(f"❌ 第 {lineno} 行物理状态无法自动判定，转人工核对（不做任何改动）：")
            print(f"   源位置：{source_path}")
            print(f"   目标位置：{target_path}")
            return EXIT_MANUAL
    return None


def _pending(
    moves: list[tuple[int, str, str]],
    states: dict[int, set[str]],
    undo: bool,
) -> list[tuple[int, str, str]]:
    if undo:
        return [
            (lineno, src, dst) for lineno, src, dst in moves
            if "DONE" in states.get(lineno, set()) and "UNDONE" not in states.get(lineno, set())
        ]
    return [(lineno, src, dst) for lineno, src, dst in moves if "DONE" not in states.get(lineno, set())]


def _apply_moves(
    moves: list[tuple[int, str, str]],
    states: dict[int, set[str]],
    log_path: Path,
    root: Path,
) -> int:
    done = 0
    for lineno, src, dst in moves:
        if "DONE" in states.get(lineno, set()):
            continue
        _log(log_path, "BEGIN", lineno, src, dst)
        try:
            _move_noreplace(root / src, root / dst)
        except OSError as exc:
            print(f"❌ 第 {lineno} 行移动失败：{src} → {dst}（{exc}）")
            print(f"   已完成 {done} 行，已完成部分保留；可用 --undo 撤销。")
            return EXIT_FAILED
        _log(log_path, "DONE", lineno, src, dst)
        done += 1
    print(f"✅ 移动完成：{done} 行。")
    return EXIT_OK


def _undo_moves(
    moves: list[tuple[int, str, str]],
    states: dict[int, set[str]],
    log_path: Path,
    root: Path,
) -> int:
    undone = 0
    for lineno, src, dst in reversed(_pending(moves, states, undo=True)):
        _log(log_path, "UNDO_BEGIN", lineno, src, dst)
        try:
            _move_noreplace(root / dst, root / src)
        except OSError as exc:
            print(f"❌ 第 {lineno} 行撤销失败：{dst} → {src}（{exc}）")
            print(f"   已撤销 {undone} 行；解除冲突后再次运行会先做恢复核对并跳过已撤销行。")
            return EXIT_FAILED
        _log(log_path, "UNDONE", lineno, src, dst)
        undone += 1
    print(f"✅ 撤销完成：{undone} 行（不删除任何文件，遗留空目录不清理）。")
    return EXIT_OK


def run(manifest_path: Path, *, root: Path, apply: bool = False, undo: bool = False) -> int:
    root = root.expanduser().resolve()
    if not manifest_path.is_file():
        print(f"❌ 清单不存在：{manifest_path}")
        return EXIT_PRECHECK
    moves, errors = _read_manifest(manifest_path)
    if errors:
        for error in errors:
            print(f"❌ {error}")
        return EXIT_PRECHECK

    log_path = manifest_path.parent / (manifest_path.name + ".log")
    states = _read_log(log_path)
    problems = _precheck(moves, states, root, undo)
    if problems:
        print(f"❌ 预检拒绝，未做任何改动（{len(problems)} 项）：")
        for problem in problems:
            print(f"  • {problem}")
        return EXIT_PRECHECK

    if not apply:
        pending = _pending(moves, states, undo)
        verb, arrow = ("撤销", "目标 → 源") if undo else ("移动", "源 → 目标")
        print(f"📋 dry-run：待{verb} {len(pending)} 行（{arrow}），未做任何改动；加 --apply 执行：")
        for lineno, src, dst in pending:
            print(f"  第 {lineno} 行：{dst} → {src}" if undo else f"  第 {lineno} 行：{src} → {dst}")
        return EXIT_OK

    # 撤销前先核对正向半完成行（已移动未记 DONE 的行须先补记，才进入撤销射程）
    for direction in ((False, True) if undo else (False,)):
        manual = _recover(moves, states, log_path, root, direction)
        if manual is not None:
            return manual
    if undo:
        return _undo_moves(moves, states, log_path, root)
    return _apply_moves(moves, states, log_path, root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按 moves.tsv 无覆盖移动目录/文件（默认 dry-run；--apply 执行；--undo 逆序撤销）",
    )
    parser.add_argument("manifest", type=Path, help="移动清单：每行「源<TAB>目标」，路径相对工作区根")
    parser.add_argument("--apply", action="store_true", help="执行（缺省仅打印计划）")
    parser.add_argument("--undo", action="store_true", help="撤销已完成的移动（逆序；执行需配合 --apply）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return run(args.manifest, root=paths.WORKSPACE_ROOT, apply=args.apply, undo=args.undo)


if __name__ == "__main__":
    sys.exit(main())
