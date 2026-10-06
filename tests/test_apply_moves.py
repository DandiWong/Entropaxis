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

    def test_race_file_target_created_after_precheck(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("原内容", encoding="utf-8")
            manifest = _write_manifest(root, [("a.txt", "b.txt")])
            real_link = os.link

            def link_racer(src, dst, **kwargs):
                Path(dst).write_text("他人写入", encoding="utf-8")  # 预检通过后、link 前被他人抢占
                return real_link(src, dst, **kwargs)

            with mock.patch.object(apply_moves.os, "link", link_racer):
                self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 1)

            self.assertEqual((root / "b.txt").read_text(encoding="utf-8"), "他人写入")
            self.assertTrue((root / "a.txt").is_file())
            self.assertEqual((root / "a.txt").read_text(encoding="utf-8"), "原内容")

    def test_race_dir_target_filled_after_claim(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "d1").mkdir()
            (root / "d1" / "inner.txt").write_text("原内容", encoding="utf-8")
            manifest = _write_manifest(root, [("d1", "d2")])
            real_rename = os.rename

            def rename_racer(src, dst):
                (Path(dst) / "他人文件.txt").write_text("他人写入", encoding="utf-8")  # 占位后被他人写入
                return real_rename(src, dst)

            with mock.patch.object(apply_moves.os, "rename", rename_racer):
                self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 1)

            self.assertEqual((root / "d2" / "他人文件.txt").read_text(encoding="utf-8"), "他人写入")
            self.assertTrue((root / "d1" / "inner.txt").is_file())
            self.assertFalse((root / "d2" / "inner.txt").exists())

    def test_recover_linked_not_unlinked(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("甲", encoding="utf-8")
            os.link(root / "a.txt", root / "b.txt")
            manifest = _write_manifest(root, [("a.txt", "b.txt")])
            log = manifest.parent / "moves.tsv.log"
            log.write_text("BEGIN\t1\ta.txt\tb.txt\n", encoding="utf-8")

            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 0)

            self.assertFalse((root / "a.txt").exists())
            self.assertEqual((root / "b.txt").read_text(encoding="utf-8"), "甲")
            self.assertIn("DONE\t1\ta.txt\tb.txt", _log_text(manifest))

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

            def flaky(src: Path, dst: Path) -> None:
                calls.append(src)
                if len(calls) == 2:
                    raise OSError("注入失败")
                real_move(src, dst)

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

    def test_recover_undo_linked_not_unlinked(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("甲", encoding="utf-8")
            manifest = _write_manifest(root, [("a.txt", "b.txt")])
            self.assertEqual(apply_moves.run(manifest, root=root, apply=True), 0)
            # 注入中断：撤销已 link 未 unlink，仅记录 UNDO_BEGIN
            os.link(root / "b.txt", root / "a.txt")
            with (manifest.parent / "moves.tsv.log").open("a", encoding="utf-8") as handle:
                handle.write("UNDO_BEGIN\t1\ta.txt\tb.txt\n")

            self.assertEqual(apply_moves.run(manifest, root=root, apply=True, undo=True), 0)

            self.assertEqual((root / "a.txt").read_text(encoding="utf-8"), "甲")
            self.assertFalse((root / "b.txt").exists())
            self.assertIn("UNDONE\t1\ta.txt\tb.txt", _log_text(manifest))

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
