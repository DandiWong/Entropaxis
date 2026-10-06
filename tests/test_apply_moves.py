import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from tools import apply_moves


def _write_manifest(root: Path, rows: list[tuple[str, str]]) -> Path:
    manifest = root / "moves.tsv"
    manifest.write_text("".join(f"{src}\t{dst}\n" for src, dst in rows), encoding="utf-8")
    return manifest


def _log_text(manifest: Path) -> str:
    log = manifest.parent / (manifest.name + ".log")
    return log.read_text(encoding="utf-8") if log.is_file() else ""


class ApplyMovesTests(TestCase):
    def test_dry_run_moves_nothing(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("甲", encoding="utf-8")
            manifest = _write_manifest(root, [("a.txt", "b/归档/a.txt")])

            self.assertEqual(apply_moves.run(manifest, root=root), 0)

            self.assertTrue((root / "a.txt").is_file())
            self.assertFalse((root / "b").exists())
            self.assertEqual(_log_text(manifest), "")

    def test_apply_then_undo_restores(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "1.txt").write_text("内容甲", encoding="utf-8")
            (root / "d1" / "inner").mkdir(parents=True)
            (root / "d1" / "inner" / "2.txt").write_text("内容乙", encoding="utf-8")
            manifest = _write_manifest(root, [("1.txt", "归档/1.txt"), ("d1", "归档/d1")])

            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 0)
            self.assertTrue((root / "归档" / "1.txt").is_file())
            self.assertTrue((root / "归档" / "d1" / "inner" / "2.txt").is_file())
            self.assertFalse((root / "1.txt").exists())
            self.assertFalse((root / "d1").exists())

            self.assertEqual(apply_moves.run(manifest, root=root, apply=True, undo=True), 0)
            self.assertEqual((root / "1.txt").read_text(encoding="utf-8"), "内容甲")
            self.assertEqual((root / "d1" / "inner" / "2.txt").read_text(encoding="utf-8"), "内容乙")
            self.assertFalse((root / "归档" / "1.txt").exists())
            self.assertFalse((root / "归档" / "d1").exists())

    def test_existing_target_rejects_whole_batch(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("a", encoding="utf-8")
            (root / "b.txt").write_text("b", encoding="utf-8")
            (root / "y").mkdir()
            (root / "y" / "b.txt").write_text("占位", encoding="utf-8")
            manifest = _write_manifest(root, [("a.txt", "x/a.txt"), ("b.txt", "y/b.txt")])

            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 2)

            self.assertTrue((root / "a.txt").is_file())
            self.assertFalse((root / "x").exists())
            self.assertEqual(_log_text(manifest), "")

    def test_path_escape_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("a", encoding="utf-8")
            manifest = _write_manifest(root, [("a.txt", "../逃逸.txt")])

            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 2)

            self.assertTrue((root / "a.txt").is_file())
            self.assertFalse((root.parent / "逃逸.txt").exists())
            self.assertEqual(_log_text(manifest), "")

    def test_shared_dir_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("a", encoding="utf-8")
            config = root / ".entropaxis" / "data" / "templates" / "workspace-config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text("shared_dirs:\n  - {dir: 00_知识库/, purpose: 共享}\n", encoding="utf-8")
            manifest = _write_manifest(root, [("a.txt", "00_知识库/a.txt")])

            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 2)

            self.assertTrue((root / "a.txt").is_file())
            self.assertEqual(_log_text(manifest), "")

    def test_duplicate_and_chained_targets_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("a.txt", "b.txt", "c.txt"):
                (root / name).write_text(name, encoding="utf-8")
            manifest = _write_manifest(root, [("a.txt", "x/a.txt"), ("b.txt", "x/a.txt")])

            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 2)

            chained = _write_manifest(root, [("a.txt", "b.txt"), ("b.txt", "c2.txt")])
            self.assertEqual(apply_moves.run(chained, root=root, apply=True), 2)
            self.assertTrue((root / "a.txt").is_file())
            self.assertFalse((root / "x").exists())

    def test_recover_renamed_not_logged(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("甲", encoding="utf-8")
            os.rename(root / "a.txt", root / "b.txt")
            manifest = _write_manifest(root, [("a.txt", "b.txt")])
            (manifest.parent / "moves.tsv.log").write_text(
                "BEGIN\t1\ta.txt\tb.txt\n", encoding="utf-8"
            )

            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 0)
            self.assertIn("DONE\t1\ta.txt\tb.txt", _log_text(manifest))

            self.assertEqual(apply_moves.run(manifest, root=root, apply=True, undo=True), 0)
            self.assertEqual((root / "a.txt").read_text(encoding="utf-8"), "甲")
            self.assertFalse((root / "b.txt").exists())

    def test_partial_failure_undo_only_done_rows(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("a", "b", "c"):
                (root / name).write_text(name, encoding="utf-8")
            manifest = _write_manifest(root, [("a", "x/a"), ("b", "x/b"), ("c", "x/c")])
            real_move = apply_moves._move_noreplace
            calls = []

            def flaky(*args) -> None:
                calls.append(args)
                if len(calls) == 2:
                    raise OSError("注入失败")
                real_move(*args)

            with mock.patch.object(apply_moves, "_move_noreplace", flaky):
                self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 1)
            self.assertTrue((root / "x" / "a").is_file())

            self.assertEqual(apply_moves.run(manifest, root=root, apply=True, undo=True), 0)
            for name in ("a", "b", "c"):
                self.assertEqual((root / name).read_text(encoding="utf-8"), name)
            self.assertFalse((root / "x" / "a").exists())

    def test_undo_recovers_forward_renamed_not_logged(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("甲", encoding="utf-8")
            os.rename(root / "a.txt", root / "b.txt")
            manifest = _write_manifest(root, [("a.txt", "b.txt")])
            (manifest.parent / "moves.tsv.log").write_text(
                "BEGIN\t1\ta.txt\tb.txt\n", encoding="utf-8"
            )

            self.assertEqual(apply_moves.run(manifest, root=root, apply=True, undo=True), 0)
            self.assertEqual((root / "a.txt").read_text(encoding="utf-8"), "甲")
            self.assertFalse((root / "b.txt").exists())

    def test_undo_retry_skips_undone(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [(f"a{i}.txt", f"b{i}.txt") for i in (1, 2, 3)]
            for src, _ in rows:
                (root / src).write_text(f"内容{src}", encoding="utf-8")
            manifest = _write_manifest(root, rows)
            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 0)

            (root / "a1.txt").write_text("冲突占位", encoding="utf-8")
            self.assertEqual(apply_moves.run(manifest, root=root, apply=True, undo=True), 1)
            # 逆序撤销：3、2 已还原，1 因原位置被占失败
            self.assertTrue((root / "a3.txt").is_file())
            self.assertTrue((root / "a2.txt").is_file())
            self.assertTrue((root / "b1.txt").is_file())

            (root / "a1.txt").unlink()  # 解除冲突
            self.assertEqual(apply_moves.run(manifest, root=root, apply=True, undo=True), 0)

            for i in (1, 2, 3):
                self.assertEqual((root / f"a{i}.txt").read_text(encoding="utf-8"), f"内容a{i}.txt")
                self.assertFalse((root / f"b{i}.txt").exists())
                self.assertEqual(_log_text(manifest).count(f"UNDONE\t{i}\t"), 1)

    def test_recover_undo_restored_not_logged(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("甲", encoding="utf-8")
            manifest = _write_manifest(root, [("a.txt", "b.txt")])
            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 0)
            # 注入中断：撤销已完成但 UNDONE 未记
            os.rename(root / "b.txt", root / "a.txt")
            with (manifest.parent / "moves.tsv.log").open("a", encoding="utf-8") as handle:
                handle.write("UNDO_BEGIN\t1\ta.txt\tb.txt\n")

            self.assertEqual(apply_moves.run(manifest, root=root, apply=True, undo=True), 0)

            self.assertEqual((root / "a.txt").read_text(encoding="utf-8"), "甲")
            self.assertFalse((root / "b.txt").exists())
            self.assertIn("UNDONE\t1\ta.txt\tb.txt", _log_text(manifest))

    def test_empty_dir_at_target_requires_manual(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("甲", encoding="utf-8")
            (root / "b").mkdir()
            manifest = _write_manifest(root, [("a.txt", "b")])
            log = manifest.parent / "moves.tsv.log"
            log.write_text("BEGIN\t1\ta.txt\tb\n", encoding="utf-8")

            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 3)

            self.assertTrue((root / "a.txt").is_file())
            self.assertTrue((root / "b").is_dir())
            self.assertEqual(list((root / "b").iterdir()), [])
            self.assertEqual(log.read_text(encoding="utf-8"), "BEGIN\t1\ta.txt\tb\n")

    # ---- 实施主审回归（C-4~C-7、M-9~M-12、N-1）----

    def _racing_rename(self, before):
        real = apply_moves._rename_noreplace

        def racer(src_fd, src_name, dst_fd, dst_name):
            before()
            return real(src_fd, src_name, dst_fd, dst_name)

        return mock.patch.object(apply_moves, "_rename_noreplace", racer)

    def test_race_file_target_created_after_precheck(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("原内容", encoding="utf-8")
            manifest = _write_manifest(root, [("a.txt", "b.txt")])
            with self._racing_rename(lambda: (root / "b.txt").write_text("他人写入", encoding="utf-8")):
                self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 1)
            self.assertEqual((root / "b.txt").read_text(encoding="utf-8"), "他人写入")
            self.assertEqual((root / "a.txt").read_text(encoding="utf-8"), "原内容")

    def test_race_dir_target_created_after_precheck(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "d1").mkdir()
            (root / "d1" / "inner.txt").write_text("原内容", encoding="utf-8")
            manifest = _write_manifest(root, [("d1", "d2")])

            def occupy() -> None:
                (root / "d2").mkdir()  # 他人抢占的空目录也不得被替换

            with self._racing_rename(occupy):
                self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 1)
            self.assertTrue((root / "d1" / "inner.txt").is_file())
            self.assertEqual(list((root / "d2").iterdir()), [])

    def test_source_replaced_before_rename_loses_nothing(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_text("original", encoding="utf-8")
            manifest = _write_manifest(root, [("a", "b")])

            def replace_source() -> None:
                (root / "tmp").write_text("concurrent-new-data", encoding="utf-8")
                os.replace(root / "tmp", root / "a")

            with self._racing_rename(replace_source):
                self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 0)
            # 原子 rename 移走的是被替换后的目录项本身，没有任何路径被单独删除
            self.assertEqual((root / "b").read_text(encoding="utf-8"), "concurrent-new-data")
            self.assertFalse((root / "a").exists())

    def _outside_fixture(self, base: Path):
        root, outside = base / "root", base / "outside"
        root.mkdir(), outside.mkdir(), (root / "out").mkdir()
        (root / "a").write_text("payload", encoding="utf-8")
        return root, outside

    def test_parent_redirected_outside_root_after_precheck(self) -> None:
        with TemporaryDirectory() as temporary:
            root, outside = self._outside_fixture(Path(temporary).resolve())
            manifest = _write_manifest(root, [("a", "out/new/a")])
            real_move = apply_moves._move_noreplace

            def redirect(*args):
                (root / "out").rmdir()
                (root / "out").symlink_to(outside, target_is_directory=True)
                return real_move(*args)

            with mock.patch.object(apply_moves, "_move_noreplace", redirect):
                self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 1)
            self.assertEqual(list(outside.iterdir()), [])  # 根外既无文件也无新建目录
            self.assertEqual((root / "a").read_text(encoding="utf-8"), "payload")

    def test_parent_relocated_outside_after_open_is_reverted(self) -> None:
        with TemporaryDirectory() as temporary:
            root, outside = self._outside_fixture(Path(temporary).resolve())
            manifest = _write_manifest(root, [("a", "out/a")])
            real = apply_moves._rename_noreplace
            calls = []

            def relocate(*args):
                calls.append(args)
                if len(calls) == 1:  # 父目录 fd 核对后、rename 前被整体迁出工作区
                    os.rename(root / "out", outside / "captured")
                return real(*args)

            with mock.patch.object(apply_moves, "_rename_noreplace", relocate):
                self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 1)
            self.assertEqual((root / "a").read_text(encoding="utf-8"), "payload")
            self.assertEqual(list((outside / "captured").iterdir()), [])

    def _write_config(self, root: Path, body: str) -> None:
        config = root / ".entropaxis" / "data" / "templates" / "workspace-config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(body, encoding="utf-8")

    def test_shared_dir_rejected_in_every_yaml_form(self) -> None:
        bodies = (
            "shared_dirs:\n  - purpose: 共享\n    dir: shared/\n",
            "shared_dirs: [{dir: shared/, purpose: 共享}]\n",
            "shared_dirs:\n  - {dir: shared/, purpose: 共享}\n",
        )
        for body in bodies:
            for rows in ([("shared/a", "private/a")], [("private/b", "shared/b")]):
                with self.subTest(body=body, rows=rows), TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / "shared").mkdir()
                    (root / "private").mkdir()
                    (root / "shared" / "a").write_text("s", encoding="utf-8")
                    (root / "private" / "b").write_text("p", encoding="utf-8")
                    self._write_config(root, body)
                    manifest = _write_manifest(root, rows)
                    self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 2)
                    self.assertTrue((root / "shared" / "a").exists())
                    self.assertTrue((root / "private" / "b").exists())
                    self.assertEqual(_log_text(manifest), "")

    def test_shared_dir_symlink_alias_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "real").mkdir()
            (root / "real" / "a").write_text("s", encoding="utf-8")
            (root / "alias").symlink_to(root / "real", target_is_directory=True)
            self._write_config(root, "shared_dirs:\n  - {dir: alias/, purpose: 共享}\n")
            manifest = _write_manifest(root, [("real/a", "private/a")])
            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 2)
            self.assertTrue((root / "real" / "a").exists())

    def test_unparseable_config_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_text("x", encoding="utf-8")
            self._write_config(root, "shared_dirs: [unclosed\n")
            manifest = _write_manifest(root, [("a", "b")])
            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 2)
            self.assertTrue((root / "a").exists())

    def test_edited_manifest_after_apply_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_text("original", encoding="utf-8")
            manifest = _write_manifest(root, [("a", "b")])
            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 0)
            (root / "d").write_text("unrelated", encoding="utf-8")
            _write_manifest(root, [("c", "d")])
            self.assertEqual(apply_moves.run(manifest, root=root, apply=True, undo=True), 2)
            self.assertEqual((root / "d").read_text(encoding="utf-8"), "unrelated")
            self.assertEqual((root / "b").read_text(encoding="utf-8"), "original")
            self.assertFalse((root / "c").exists())

    def test_equivalent_targets_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_text("a", encoding="utf-8")
            (root / "b").write_text("b", encoding="utf-8")
            manifest = _write_manifest(root, [("a", "out/c"), ("b", "out/./c")])
            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 2)
            self.assertTrue((root / "a").exists() and (root / "b").exists())
            self.assertEqual(_log_text(manifest), "")

    def test_recovery_manual_makes_no_partial_changes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "b1").write_text("one", encoding="utf-8")  # 第 1 行：已移动未记 DONE，可补记
            (root / "a2").write_text("two", encoding="utf-8")  # 第 2 行：两端都在，须人工
            (root / "b2").write_text("foreign", encoding="utf-8")
            manifest = _write_manifest(root, [("a1", "b1"), ("a2", "b2")])
            log = manifest.parent / "moves.tsv.log"
            log.write_text("BEGIN\t1\ta1\tb1\nBEGIN\t2\ta2\tb2\n", encoding="utf-8")
            before = log.read_bytes()
            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 3)
            self.assertEqual(log.read_bytes(), before)

    def test_both_ends_present_requires_manual(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("甲", encoding="utf-8")
            os.link(root / "a.txt", root / "b.txt")
            manifest = _write_manifest(root, [("a.txt", "b.txt")])
            (manifest.parent / "moves.tsv.log").write_text("BEGIN\t1\ta.txt\tb.txt\n", encoding="utf-8")
            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 3)
            self.assertTrue((root / "a.txt").exists() and (root / "b.txt").exists())

    def test_apply_recovers_incomplete_undo_intent(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_text("payload", encoding="utf-8")  # 撤销已完成、UNDONE 未落盘
            manifest = _write_manifest(root, [("a", "b")])
            (manifest.parent / "moves.tsv.log").write_text(
                "BEGIN\t1\ta\tb\nDONE\t1\ta\tb\nUNDO_BEGIN\t1\ta\tb\n", encoding="utf-8"
            )
            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 0)
            self.assertIn("UNDONE\t1\ta\tb", _log_text(manifest))
            self.assertEqual((root / "a").read_text(encoding="utf-8"), "payload")

    def test_only_empty_dir_target_requires_manual_both_directions(self) -> None:
        for undo in (False, True):
            with self.subTest(undo=undo), TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / ("a" if undo else "b")).mkdir()
                manifest = _write_manifest(root, [("a", "b")])
                log = manifest.parent / "moves.tsv.log"
                text = "BEGIN\t1\ta\tb\n" + ("DONE\t1\ta\tb\nUNDO_BEGIN\t1\ta\tb\n" if undo else "")
                log.write_text(text, encoding="utf-8")
                self.assertEqual(apply_moves.run(manifest, root=root, apply=True, undo=undo), 3)
                self.assertEqual(log.read_text(encoding="utf-8"), text)

    def test_log_failure_is_controlled(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_text("x", encoding="utf-8")
            manifest = _write_manifest(root, [("a", "b")])
            real_log = apply_moves._log

            def failing_done(path, status, *args):
                if status == "DONE":
                    raise OSError("磁盘满")
                return real_log(path, status, *args)

            with mock.patch.object(apply_moves, "_log", failing_done):
                self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 1)
            self.assertTrue((root / "b").exists())
            # 下次运行按物理状态补记 DONE，随后可撤销
            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 0)
            self.assertEqual(apply_moves.run(manifest, root=root, apply=True, undo=True), 0)
            self.assertTrue((root / "a").exists())
