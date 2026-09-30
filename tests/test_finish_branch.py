"""finish_branch.py：提交已暂存 → 合入目标 → 推送 → 删分支，失败时不留半成品。"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
import finish_branch as fb  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


class FinishBranchTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, GIT_ENV)
        self._env.start()
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.origin = root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.repo = root / "repo"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True, capture_output=True)
        self.write("base.txt", "base\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")
        self.git("push", "-q", "-u", "origin", "main")
        self.git("checkout", "-q", "-b", "uat")
        self.git("push", "-q", "-u", "origin", "uat")
        self.git("checkout", "-q", "-b", "feat")
        self.write("feat.txt", "feat\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "feat")

    def tearDown(self):
        self._tmp.cleanup()
        self._env.stop()

    def write(self, name, text):
        (self.repo / name).write_text(text, encoding="utf-8")

    def git(self, *args, repo=None):
        return subprocess.run(["git", "-C", str(repo or self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def rev(self, ref, repo=None):
        p = subprocess.run(["git", "-C", str(repo or self.repo), "rev-parse", "-q", "--verify", ref],
                           capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else None

    def finish(self, target="uat", message=None, delete_remote=False):
        return fb.finish_branch(fb.plan_finish(self.repo, target, "origin", message), delete_remote)

    def test_success_commits_staged_merges_pushes_and_deletes(self):
        self.write("more.txt", "more\n")
        self.git("add", "more.txt")
        self.write("scratch.txt", "untracked\n")
        result = self.finish(message="feat: more")
        self.assertTrue(result["ok"])
        self.assertEqual(self.git("branch", "--show-current"), "uat")
        self.assertIsNone(self.rev("refs/heads/feat"))
        self.assertEqual(self.rev("refs/heads/uat"), self.rev("refs/heads/uat", repo=self.origin))
        files = self.git("ls-tree", "--name-only", "uat", repo=self.origin).split()
        self.assertIn("more.txt", files)
        self.assertNotIn("scratch.txt", files, "未跟踪文件不得被提交")
        self.assertIn("scratch.txt", result["warning"])

    def test_unstaged_tracked_change_refused_without_writes(self):
        self.write("feat.txt", "changed\n")
        before = self.rev("refs/heads/feat")
        with self.assertRaises(fb.ToolError):
            fb.plan_finish(self.repo, "uat", "origin", "msg")
        self.assertEqual(self.rev("refs/heads/feat"), before)

    def test_staged_without_message_refused(self):
        self.write("more.txt", "more\n")
        self.git("add", "more.txt")
        with self.assertRaises(fb.ToolError):
            fb.plan_finish(self.repo, "uat", "origin", None)

    def test_target_same_as_source_refused(self):
        with self.assertRaises(fb.ToolError):
            fb.plan_finish(self.repo, "feat", "origin", None)

    def test_conflict_aborts_and_returns_to_source(self):
        self.git("checkout", "-q", "uat")
        self.write("feat.txt", "uat side\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "uat conflicting")
        uat_before = self.rev("refs/heads/uat")
        self.git("checkout", "-q", "feat")
        with self.assertRaises(fb.ToolError) as ctx:
            self.finish()
        self.assertIn("冲突", str(ctx.exception))
        self.assertEqual(self.git("branch", "--show-current"), "feat")
        self.assertEqual(self.rev("refs/heads/uat"), uat_before)
        self.assertIsNotNone(self.rev("refs/heads/feat"))

    def test_push_failure_keeps_source_branch(self):
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho rejected >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        with self.assertRaises(fb.ToolError) as ctx:
            self.finish()
        self.assertIn("未删除", str(ctx.exception))
        self.assertIsNotNone(self.rev("refs/heads/feat"))

    def test_diverged_local_target_refused_before_merge(self):
        other = Path(self._tmp.name) / "other"
        subprocess.run(["git", "clone", "-q", "-b", "uat", str(self.origin), str(other)], check=True, capture_output=True)
        (other / "remote.txt").write_text("r\n", encoding="utf-8")
        self.git("add", ".", repo=other)
        self.git("commit", "-q", "-m", "remote side", repo=other)
        self.git("push", "-q", "origin", "uat", repo=other)
        self.git("checkout", "-q", "uat")
        self.write("local.txt", "l\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "local side")
        local_uat = self.rev("refs/heads/uat")
        self.git("checkout", "-q", "feat")
        with self.assertRaises(fb.ToolError) as ctx:
            self.finish()
        self.assertIn("分叉", str(ctx.exception))
        self.assertEqual(self.rev("refs/heads/uat"), local_uat)
        self.assertEqual(self.git("branch", "--show-current"), "feat")

    def test_remote_source_branch_deleted_only_on_request(self):
        self.git("push", "-q", "-u", "origin", "feat")
        result = self.finish()
        self.assertIsNotNone(self.rev("refs/heads/feat", repo=self.origin))
        self.assertIn("--delete-remote", result["warning"])
        self.git("checkout", "-q", "-b", "feat2")
        self.write("f2.txt", "x\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "f2")
        self.git("push", "-q", "-u", "origin", "feat2")
        result = self.finish(delete_remote=True)
        self.assertTrue(result["remote_deleted"])
        self.assertIsNone(self.rev("refs/heads/feat2", repo=self.origin))

    def test_target_only_on_remote_is_tracked(self):
        self.git("branch", "-D", "uat")
        result = self.finish()
        self.assertTrue(result["pushed"])
        self.assertEqual(self.rev("refs/heads/uat"), self.rev("refs/heads/uat", repo=self.origin))

    def test_list_targets_excludes_source_and_puts_trunk_first(self):
        result = fb.list_targets(self.repo, "origin")
        self.assertEqual(result["source"], "feat")
        self.assertEqual(result["targets"][0], "main")
        self.assertIn("uat", result["targets"])
        self.assertNotIn("feat", result["targets"])


if __name__ == "__main__":
    unittest.main()
