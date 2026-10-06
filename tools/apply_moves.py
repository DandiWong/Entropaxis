#!/usr/bin/env python3
"""存量目录整理执行器：按 moves.tsv 原子无覆盖移动，BEGIN/DONE 日志可撤销。

用法（路径一律相对工作区根）：

    python3 .entropaxis/tools/apply_moves.py <moves.tsv>                 # dry-run，打印计划
    python3 .entropaxis/tools/apply_moves.py <moves.tsv> --apply
    python3 .entropaxis/tools/apply_moves.py <moves.tsv> --undo [--apply]

清单每行「源<TAB>目标」，空行与 # 注释行忽略；日志 `<清单>.log` 逐条 fsync，
状态取 BEGIN/DONE/UNDO_BEGIN/UNDONE，两个方向都是先记意图、后动文件。
退出码：0 成功 / 1 执行中冲突或异常（已完成部分保留）/ 2 预检拒绝（零改动）/ 3 待人工核对（零改动）。

移动原语是内核级原子「目标存在即失败」的 rename（macOS renameatx_np RENAME_EXCL、
Linux renameat2 RENAME_NOREPLACE）；父目录自工作区根逐层 O_NOFOLLOW 以 fd 打开（缺失层相对 fd
创建），执行前后复核真实路径仍在工作区内、不在共享资料目录内；因此不存在"已复制未删源"之类的中间态。
"""

from __future__ import annotations

import argparse
import ctypes
import os
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
_RENAME_EXCL_DARWIN = 0x4
_RENAME_NOREPLACE_LINUX = 0x1
_F_GETPATH_DARWIN = 50

Move = tuple[int, str, str]


class ScopeError(OSError):
    """执行时父目录真实路径越出工作区或落入共享资料目录。"""


# ---------- 平台原语 ----------

def platform_supported() -> bool:
    # ponytail: 只支持 macOS / Linux；其他平台无原子 noreplace + 目录 fd 路径查询，整单拒绝而非降级
    return sys.platform == "darwin" or sys.platform.startswith("linux")


def _fd_path(fd: int) -> Path:
    if sys.platform == "darwin":
        import fcntl  # noqa: PLC0415

        raw = fcntl.fcntl(fd, _F_GETPATH_DARWIN, b"\0" * 1024)
        return Path(raw.split(b"\0", 1)[0].decode())
    return Path(os.readlink(f"/proc/self/fd/{fd}"))


def _rename_noreplace(src_fd: int, src_name: str, dst_fd: int, dst_name: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        func, flag = libc.renameatx_np, _RENAME_EXCL_DARWIN
    else:
        func, flag = libc.renameat2, _RENAME_NOREPLACE_LINUX
    func.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if func(src_fd, os.fsencode(src_name), dst_fd, os.fsencode(dst_name), flag) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), dst_name)


def _within(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def _check_scope(fd: int, root: Path, shared: list[Path]) -> None:
    real = _fd_path(fd)
    if not _within(real, root):
        raise ScopeError(f"目录真实路径越出工作区：{real}")
    if any(_within(real, d) for d in shared):
        raise ScopeError(f"目录真实路径落在共享资料目录内：{real}")


def _open_dir_chain(root: Path, rel_dir: Path, shared: list[Path], *, create: bool) -> int:
    """自工作区根逐层以 fd 打开 rel_dir：每层 O_NOFOLLOW（不跟随符号链接），缺失层用
    相对 fd 的 mkdir 创建，最后复核真实路径。路径在核对后被替换为符号链接也无法把创建或
    打开引到根外；工作区内的符号链接目录因此一律拒绝（fail-closed）。"""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if _fd_path(fd) != root:
            raise ScopeError(f"工作区根真实路径已变化：{_fd_path(fd)}")
        for part in rel_dir.parts:
            if part in ("", ".", ".."):
                raise ScopeError(f"非法路径分量：{rel_dir}")
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        _check_scope(fd, root, shared)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _move_noreplace(source: str, target: str, root: Path, shared: list[Path]) -> None:
    """原子无覆盖移动（路径相对工作区根）：目标存在即失败；父目录逐层 fd 打开并复核作用域。

    rename 后再复核目标父目录真实路径：若其在核对后被并发进程整体迁出工作区，立即原子
    移回并报错，不静默成功。
    ponytail: 核对与 rename 之间目录被整体迁出的微秒级窗口无法在 POSIX 上消除，只能事后
    检测并移回；威胁模型不含同用户并发进程恶意搬移工作区目录（已由用户豁免，见审计 C-6）。
    """
    src_rel, dst_rel = Path(source), Path(target)
    src_fd = _open_dir_chain(root, src_rel.parent, shared, create=False)
    try:
        dst_fd = _open_dir_chain(root, dst_rel.parent, shared, create=True)
        try:
            _rename_noreplace(src_fd, src_rel.name, dst_fd, dst_rel.name)
            try:
                _check_scope(dst_fd, root, shared)
            except ScopeError as escaped:
                try:
                    _rename_noreplace(dst_fd, dst_rel.name, src_fd, src_rel.name)
                except OSError as revert_exc:
                    raise ScopeError(
                        f"移动后目标目录已不在工作区内且移回失败，须人工处理：{_fd_path(dst_fd)}/{dst_rel.name}"
                        f"（{escaped}；{revert_exc}）"
                    ) from revert_exc
                raise ScopeError(f"移动后目标目录已不在工作区内，已移回原位：{escaped}") from escaped
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


# ---------- 清单、配置与日志 ----------

def _read_manifest(manifest_path: Path) -> tuple[list[Move], list[str]]:
    """解析清单，返回 (移动行, 格式错误)；路径做词法规范化（`a/./b` → `a/b`）。"""
    moves: list[Move] = []
    errors: list[str] = []
    for lineno, raw in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            errors.append(f"第 {lineno} 行不是「源<TAB>目标」两列：{stripped}")
            continue
        moves.append((lineno, os.path.normpath(parts[0].strip()), os.path.normpath(parts[1].strip())))
    return moves, errors


def _shared_dirs(root: Path) -> list[Path] | None:
    """workspace-config.yaml 的 shared_dirs（resolve 后）。配置缺失视为无共享层；
    配置存在但无法解析返回 None，由预检整单拒绝（安全边界 fail-closed）。"""
    config = root / paths.SYSTEM_DIRNAME / "data" / "templates" / "workspace-config.yaml"
    if not config.is_file():
        return []
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        return [
            (root / str(entry["dir"]).strip().strip("/")).resolve()
            for entry in data.get("shared_dirs") or []
            if isinstance(entry, dict) and str(entry.get("dir") or "").strip()
        ]
    except Exception:
        return None


def _read_log(log_path: Path) -> list[tuple[str, int, str, str]]:
    entries: list[tuple[str, int, str, str]] = []
    if not log_path.is_file():
        return entries
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        parts = raw.split("\t")
        if len(parts) != 4 or parts[0] not in LOG_STATUSES:
            continue
        try:
            entries.append((parts[0], int(parts[1]), parts[2], parts[3]))
        except ValueError:
            continue
    return entries


def _log(log_path: Path, status: str, lineno: int, src: str, dst: str) -> None:
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{status}\t{lineno}\t{src}\t{dst}\n")
        handle.flush()
        os.fsync(handle.fileno())


# ---------- 预检 ----------

def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not os.path.lexists(current):
        if current.parent == current:
            return current
        current = current.parent
    return current


def _precheck(
    moves: list[Move],
    log_entries: list[tuple[str, int, str, str]],
    states: dict[int, set[str]],
    root: Path,
    shared: list[Path] | None,
    undo: bool,
) -> list[str]:
    """整单预检：任一问题即拒绝、零移动。存在性只查无日志标记的正向未动行——
    已有标记的行由恢复核对判定；撤销只处理已完成行，占用冲突由原子原语在执行时拒绝。"""
    if shared is None:
        return ["workspace-config.yaml 存在但无法解析 shared_dirs，无法确认共享资料边界"]
    problems: list[str] = []

    rows = {lineno: (src, dst) for lineno, src, dst in moves}
    for status, lineno, src, dst in log_entries:
        if rows.get(lineno) != (src, dst):
            problems.append(
                f"日志 {status} 第 {lineno} 行（{src} → {dst}）与当前清单不一致；"
                "清单在执行后被改动，恢复原清单或移走旧日志后再运行"
            )

    resolved_sources: dict[Path, int] = {}
    resolved_targets: dict[Path, int] = {}
    for lineno, src, dst in moves:
        for label, value, bucket in (("源", src, resolved_sources), ("目标", dst, resolved_targets)):
            if Path(value).is_absolute():
                problems.append(f"第 {lineno} 行{label}是绝对路径：{value}")
                continue
            resolved = (root / value).resolve()
            if resolved == root or not _within(resolved, root):
                problems.append(f"第 {lineno} 行{label}越出工作区根：{value}")
                continue
            if any(_within(resolved, d) for d in shared):
                problems.append(f"第 {lineno} 行{label}落在共享资料目录内（共享层由用户手工移动）：{value}")
            if resolved in bucket:
                problems.append(f"第 {lineno} 行{label}与第 {bucket[resolved]} 行指向同一位置：{value}")
            else:
                bucket[resolved] = lineno
    for path, lineno in resolved_targets.items():
        if path in resolved_sources and resolved_sources[path] != lineno:
            problems.append(f"第 {lineno} 行目标是第 {resolved_sources[path]} 行的源")

    if undo:
        return problems
    for lineno, src, dst in moves:
        if states.get(lineno):
            continue
        source_path, target_path = root / src, root / dst
        if not os.path.lexists(source_path):
            problems.append(f"第 {lineno} 行源不存在：{src}")
        if os.path.lexists(target_path):
            problems.append(f"第 {lineno} 行目标已存在：{dst}")
        try:
            if (
                _nearest_existing_parent(source_path).stat().st_dev
                != _nearest_existing_parent(target_path).stat().st_dev
            ):
                problems.append(f"第 {lineno} 行源与目标最近已存在父目录不在同一设备：{src} → {dst}")
        except OSError as exc:
            problems.append(f"第 {lineno} 行无法判定设备归属：{exc}")
    return problems


# ---------- 恢复核对 ----------

def _is_empty_dir(path: Path) -> bool:
    try:
        return path.is_dir() and not path.is_symlink() and not any(path.iterdir())
    except OSError:
        return False


def _recover(moves: list[Move], states: dict[int, set[str]], log_path: Path, root: Path) -> int | None:
    """两个方向的「有意图无完成」行先全部判定，任一无法判定即整批零改动退出 3；
    全部可判定后才补记完成标记。原子 rename 只有"未动/已动"两态，其余一律转人工。"""
    fixes: list[tuple[str, int, str, str]] = []
    manual: list[str] = []
    for undo in (False, True):
        begin_marker, done_marker = ("UNDO_BEGIN", "UNDONE") if undo else ("BEGIN", "DONE")
        for lineno, src, dst in moves:
            marks = states.get(lineno, set())
            if begin_marker not in marks or done_marker in marks:
                continue
            source, target = (dst, src) if undo else (src, dst)
            source_path, target_path = root / source, root / target
            source_exists, target_exists = os.path.lexists(source_path), os.path.lexists(target_path)
            if source_exists and not target_exists:
                continue  # 未执行，按正常流程重做
            if target_exists and not source_exists and not _is_empty_dir(target_path):
                fixes.append((done_marker, lineno, src, dst))
                continue
            # 含目标为空目录（不推断归属）、两端都在、两端都不存在
            direction = "撤销" if undo else "正向"
            manual.append(f"第 {lineno} 行（{direction}）源位置 {source_path}；目标位置 {target_path}")
    if manual:
        print("❌ 以下行物理状态无法自动判定，转人工核对（未做任何改动）：")
        for item in manual:
            print(f"   {item}")
        return EXIT_MANUAL
    for marker, lineno, src, dst in fixes:
        _log(log_path, marker, lineno, src, dst)
        states.setdefault(lineno, set()).add(marker)
    return None


# ---------- 执行 ----------

def _pending(moves: list[Move], states: dict[int, set[str]], undo: bool) -> list[Move]:
    if undo:
        return [
            row for row in moves
            if "DONE" in states.get(row[0], set()) and "UNDONE" not in states.get(row[0], set())
        ]
    return [row for row in moves if "DONE" not in states.get(row[0], set())]


def _execute(rows: list[Move], log_path: Path, root: Path, shared: list[Path], undo: bool) -> int:
    begin_marker, done_marker = ("UNDO_BEGIN", "UNDONE") if undo else ("BEGIN", "DONE")
    verb = "撤销" if undo else "移动"
    done = 0
    for lineno, src, dst in rows:
        source, target = (dst, src) if undo else (src, dst)
        stage = f"记 {begin_marker}"
        try:
            _log(log_path, begin_marker, lineno, src, dst)
            stage = verb
            _move_noreplace(source, target, root, shared)
            stage = f"记 {done_marker}"
            _log(log_path, done_marker, lineno, src, dst)
        except OSError as exc:
            print(f"❌ 第 {lineno} 行{stage}失败：{source} → {target}（{exc}）")
            if stage == f"记 {done_marker}":
                print("   该行已移动但完成标记未落盘；下次运行会按物理状态补记。")
            hint = "解除冲突后再次 --undo 会先做恢复核对并跳过已撤销行。" if undo else "可用 --undo 撤销。"
            print(f"   已{verb} {done} 行，已完成部分保留；{hint}")
            return EXIT_FAILED
        done += 1
    suffix = "（不删除任何文件，遗留空目录不清理）" if undo else ""
    print(f"✅ {verb}完成：{done} 行{suffix}。")
    return EXIT_OK


def run(manifest_path: Path, *, root: Path, apply: bool = False, undo: bool = False) -> int:
    root = root.expanduser().resolve()
    if not platform_supported():
        print(f"❌ 当前平台 {sys.platform} 不支持原子无覆盖移动，未做任何改动。")
        return EXIT_PRECHECK
    if not manifest_path.is_file():
        print(f"❌ 清单不存在：{manifest_path}")
        return EXIT_PRECHECK
    moves, errors = _read_manifest(manifest_path)
    if errors:
        for error in errors:
            print(f"❌ {error}")
        return EXIT_PRECHECK

    log_path = manifest_path.parent / (manifest_path.name + ".log")
    log_entries = _read_log(log_path)
    states: dict[int, set[str]] = {}
    for status, lineno, _, _ in log_entries:
        states.setdefault(lineno, set()).add(status)
    shared = _shared_dirs(root)
    problems = _precheck(moves, log_entries, states, root, shared, undo)
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

    try:
        manual = _recover(moves, states, log_path, root)
    except OSError as exc:
        print(f"❌ 恢复核对补记日志失败（{exc}）；请检查日志文件可写后重试。")
        return EXIT_FAILED
    if manual is not None:
        return manual
    rows = _pending(moves, states, undo)
    return _execute(list(reversed(rows)) if undo else rows, log_path, root, shared, undo)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按 moves.tsv 原子无覆盖移动目录/文件（默认 dry-run；--apply 执行；--undo 逆序撤销）",
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
