import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SYSTEM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

import resume_context as RC  # noqa: E402

TOOL = SYSTEM_ROOT / "tools" / "resume_context.py"


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


class ResumeContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        base = Path(self._td.name).resolve()  # macOS 的 /var 是 /private/var 的软链
        self.ws = base / "ws"
        self.home = base / "home"
        (self.ws / "proj" / "docs").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                                                "CODEX_HOME": str(self.home / ".codex")})
        self.env.start()
        self.trace = base / "trace.jsonl"

    def tearDown(self) -> None:
        self.env.stop()
        self._td.cleanup()

    def run_rc(self, root: Path | None = None, **kw) -> dict:
        return RC.resume_context(root or self.ws, trace=self.trace, workspace=self.ws, **kw)

    def claude_session(self, sid: str, cwd: Path, user: str, assistant: str) -> None:
        enc = "".join(c if c.isalnum() and c.isascii() else "-" for c in str(cwd))
        _jsonl(self.home / ".claude" / "projects" / enc / f"{sid}.jsonl", [
            {"type": "user", "cwd": str(cwd), "message": {"content": user}},
            {"type": "user", "cwd": str(cwd), "message": {"content": "<command-name>/clear</command-name>"}},
            {"type": "assistant", "cwd": str(cwd), "message": {"content": [{"type": "text", "text": assistant}]}},
        ])

    def test_empty_workspace_is_none(self) -> None:
        self.assertEqual(self.run_rc()["verdict"], "none")

    def test_last_session_tail_skips_noise_and_current(self) -> None:
        self.claude_session("old", self.ws / "proj", "拆", "Tasks.md 拆好了")
        self.claude_session("current", self.ws / "proj", "继续", "当前会话")
        res = self.run_rc(exclude="current")
        self.assertEqual(res["verdict"], "unique")
        [s] = res["signals"]["session"]
        self.assertEqual((s["user"], s["assistant"]), ("拆", "Tasks.md 拆好了"), "注入的命令消息不算用户发言")

    def test_codex_session_filtered_by_cwd(self) -> None:
        d = self.home / ".codex" / "sessions" / "2026" / "10" / "06"
        _jsonl(d / "a.jsonl", [{"type": "session_meta", "payload": {"id": "a", "cwd": str(self.ws / "proj")}},
                               {"payload": {"type": "message", "role": "user",
                                            "content": [{"type": "input_text", "text": "写 Spec"}]}}])
        _jsonl(d / "b.jsonl", [{"type": "session_meta", "payload": {"id": "b", "cwd": "/elsewhere"}}])
        [s] = self.run_rc()["signals"]["session"]
        self.assertEqual((s["source"], s["user"]), ("codex", "写 Spec"))

    def test_dispatch_start_without_end(self) -> None:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        _jsonl(self.trace, [
            {"ts": "2026-10-06T00:00:00", "event": "start", "dispatch_id": "x", "pid": os.getpid(), "role": "Reviewer"},
            {"ts": "2026-10-06T00:00:00", "event": "start", "dispatch_id": "y", "pid": dead.pid, "role": "Reviewer"},
            {"ts": "2026-10-06T00:00:00", "event": "start", "dispatch_id": "z", "pid": dead.pid, "role": "Reviewer"},
            {"ts": "2026-10-06T00:01:00", "dispatch_id": "z", "role": "Reviewer", "exit_code": 0},
        ])
        states = sorted(d["state"] for d in self.run_rc(limit=5)["signals"]["dispatch"])
        self.assertEqual(states, ["interrupted", "running"], "已配对的调度不报；无结束记录按 pid 存活区分")

    def test_tasks_capsule_rawinput_and_multiple(self) -> None:
        (self.ws / "proj" / "docs" / "Tasks.md").write_text(
            "# T\n\n## Active\n\n- [ ] `Task-1`: 骨架\n\n## Backlog\n\n- [ ] `Task-2`: 原型\n", encoding="utf-8")
        cap = self.ws / "proj" / "20261006_主题"
        cap.mkdir()
        (cap / "capsule.yaml").write_text("id: T-1\n", encoding="utf-8")
        (cap / "02_方案.md").write_text("x", encoding="utf-8")
        (self.ws / "other" / "RawInput").mkdir(parents=True)
        (self.ws / "other" / "RawInput" / "a.pdf").write_text("x", encoding="utf-8")
        (self.ws / "other" / "RawInput" / ".gitkeep").write_text("", encoding="utf-8")
        res = self.run_rc()
        self.assertEqual(res["signals"]["tasks"][0]["active"], ["[ ] `Task-1`: 骨架"])
        self.assertEqual(res["signals"]["capsule"][0]["stages"], ["02_方案.md"])
        self.assertEqual(res["signals"]["rawinput"][0]["items"], 1)
        self.assertEqual((res["verdict"], res["projects"]), ("multiple", ["other", "proj"]))
        self.assertEqual(self.run_rc(root=self.ws / "proj")["verdict"], "unique", "扫描单个项目时子目录不算多个项目")

    def test_stale_capsule_ignored(self) -> None:
        cap = self.ws / "proj" / "20250101_旧"
        cap.mkdir()
        (cap / "capsule.yaml").write_text("id: T-0\n", encoding="utf-8")
        old = time.time() - 30 * 86400
        os.utime(cap / "capsule.yaml", (old, old))
        self.assertEqual(self.run_rc()["signals"]["capsule"], [])

    def test_default_root(self) -> None:
        self.assertEqual(RC.default_root(self.ws / "proj" / "docs", self.ws), self.ws / "proj")
        self.assertEqual(RC.default_root(self.ws, self.ws), self.ws)
        self.assertEqual(RC.default_root(Path("/"), self.ws), self.ws)

    def test_cli_json_and_bad_input(self) -> None:
        ok = subprocess.run([sys.executable, str(TOOL), "--root", str(self.ws), "--json"],
                            capture_output=True, text=True, env=os.environ)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("verdict", json.loads(ok.stdout))
        bad = subprocess.run([sys.executable, str(TOOL), "--root", str(self.ws / "nope")],
                             capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("👉", bad.stderr)


if __name__ == "__main__":
    unittest.main()
