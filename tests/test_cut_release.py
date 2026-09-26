"""cut_release.py：隔离合并 → 验证 → 主干与 tag 单事务更新（不推送）。"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
import cut_release as cr  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
PASS = f'{sys.executable} -c "import pathlib,sys; sys.exit(0 if pathlib.Path(\'feat.txt\').exists() else 1)"'
FAIL = f'{sys.executable} -c "import sys; print(\'boom\'); sys.exit(3)"'


class CutReleaseTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, GIT_ENV)
        self._env.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")
        self.git("checkout", "-q", "-b", "feat")
        (self.repo / "feat.txt").write_text("feat\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "feat")
        self.git("checkout", "-q", "main")
        self.main_before = self.rev("refs/heads/main")

    def tearDown(self):
        self._tmp.cleanup()
        self._env.stop()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def rev(self, ref):
        p = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "-q", "--verify", ref],
                           capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else None

    def release(self, version="v1.0.0", verify=PASS):
        return cr.cut_release(cr.plan_release(self.repo, version, "feat", None), verify, None, 60)

    def test_success_moves_main_tags_and_syncs_checkout(self):
        result = self.release()
        self.assertTrue(result["ok"])
        self.assertFalse(result["pushed"])
        new_main = self.rev("refs/heads/main")
        self.assertNotEqual(new_main, self.main_before)
        self.assertEqual(self.rev("refs/tags/v1.0.0^{commit}"), new_main)
        self.assertEqual(self.git("cat-file", "-t", "refs/tags/v1.0.0"), "tag")  # 附注 tag
        self.assertTrue((self.repo / "feat.txt").exists(), "检出主干的工作区文件须同步")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertNotIn("cut-release-", self.git("worktree", "list"))

    def test_verify_failure_leaves_main_and_keeps_log(self):
        with self.assertRaises(cr.ToolError) as ctx:
            self.release(verify=FAIL)
        self.assertEqual(self.rev("refs/heads/main"), self.main_before)
        self.assertIsNone(self.rev("refs/tags/v1.0.0"))
        log = Path(str(ctx.exception).split("日志: ")[1].split("\n")[0])
        self.assertIn("boom", log.read_text(encoding="utf-8"))
        shutil.rmtree(log.parent)

    def test_existing_tag_refused(self):
        self.git("tag", "v1.0.0")
        with self.assertRaises(cr.ToolError):
            cr.plan_release(self.repo, "v1.0.0", "feat", None)

    def test_version_must_increase(self):
        self.git("tag", "v2.0.0")
        with self.assertRaises(cr.ToolError):
            cr.plan_release(self.repo, "v1.9.9", "feat", None)

    def test_invalid_version_refused(self):
        with self.assertRaises(cr.ToolError):
            cr.plan_release(self.repo, "1.0", "feat", None)

    def test_dirty_checked_out_main_refused(self):
        (self.repo / "base.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(cr.ToolError):
            cr.plan_release(self.repo, "v1.0.0", "feat", None)

    def test_conflict_aborts_without_change(self):
        (self.repo / "feat.txt").write_text("main side\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "main conflicting")
        before = self.rev("refs/heads/main")
        with self.assertRaises(cr.ToolError) as ctx:
            self.release()
        self.assertIn("冲突", str(ctx.exception))
        self.assertEqual(self.rev("refs/heads/main"), before)
        self.assertIsNone(self.rev("refs/tags/v1.0.0"))

    def test_plan_reports_pending_without_writing(self):
        plan = cr.plan_release(self.repo, "v1.0.0", "feat", None)
        self.assertEqual(len(plan["pending"]), 1)
        self.assertEqual(self.rev("refs/heads/main"), self.main_before)


if __name__ == "__main__":
    unittest.main()
