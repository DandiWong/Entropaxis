"""Windows exe 安装包：载荷导出侧与安装侧的契约测试。

覆盖三处容易静默失效的地方：安装只写 `.entropaxis/` 不碰 `.entropaxis/data/`、越界成员路径必须阻断、
以及初始化不得经 `sys.executable` 起子进程（冻结后那会递归重启安装程序）。
"""

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT / "tools"))

import build_windows_installer as builder  # noqa: E402
import install_windows as installer  # noqa: E402


def _fake_payload(extra: dict[str, str] | None = None) -> bytes:
    """造一份最小可用的控制面载荷。"""
    members = {
        "tools/bootstrap.py": "print('ok')\n",
        "entrypoints/AGENTS.md": "# 入口\n",
        "entrypoints/CLAUDE.md": "# 入口\n",
    }
    members.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return buffer.getvalue()


class InstallTests(unittest.TestCase):
    def test_install_writes_system_dir_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "工作区"
            root.mkdir()
            (root / installer.SYSTEM_DIRNAME / "data").mkdir(parents=True)
            (root / installer.SYSTEM_DIRNAME / "data" / "凭据.md").write_text("用户数据", encoding="utf-8")

            res = installer.install(_fake_payload(), root)

            self.assertEqual(res["mode"], "install")
            self.assertTrue((root / installer.SYSTEM_DIRNAME / "tools" / "bootstrap.py").is_file())
            self.assertEqual((root / installer.SYSTEM_DIRNAME / "data" / "凭据.md").read_text(encoding="utf-8"), "用户数据")

    def test_reinstall_keeps_local_private_skills(self) -> None:
        """覆盖合并而非整目录替换：收件方自己放的私有能力不能被升级抹掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installer.install(_fake_payload(), root)
            private = root / installer.SYSTEM_DIRNAME / "skills" / "私有能力" / "SKILL.md"
            private.parent.mkdir(parents=True)
            private.write_text("本机私有", encoding="utf-8")

            res = installer.install(_fake_payload(), root)

            self.assertEqual(res["mode"], "upgrade")
            self.assertTrue(private.is_file())

    def test_choosing_system_dir_itself_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wrong = Path(tmp) / installer.SYSTEM_DIRNAME
            wrong.mkdir()
            with self.assertRaises(installer.ToolError) as ctx:
                installer.install(_fake_payload(), wrong)
            self.assertIn("上一层", str(ctx.exception))

    def test_incomplete_payload_is_blocked(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("rules/00_元规则.md", "# 规则\n")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(installer.ToolError) as ctx:
                installer.install(buffer.getvalue(), Path(tmp))
            self.assertIn("tools/bootstrap.py", str(ctx.exception))

    def test_zip_slip_member_is_blocked(self) -> None:
        payload = _fake_payload({"../逃逸.md": "越界"})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "工作区"
            with self.assertRaises(installer.ToolError) as ctx:
                installer.install(payload, root)
            self.assertIn("越界路径", str(ctx.exception))
            self.assertFalse((Path(tmp) / "逃逸.md").exists())


class PayloadResolutionTests(unittest.TestCase):
    """载荷只能来自冻结 exe 的随包数据；非冻结形态必须给出可操作的阻断。"""

    def tearDown(self) -> None:
        if hasattr(sys, "_MEIPASS"):
            del sys._MEIPASS

    def test_frozen_bundle_is_used(self) -> None:
        payload = _fake_payload()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / installer.PAYLOAD_NAME).write_bytes(payload)
            sys._MEIPASS = tmp
            self.assertEqual(installer.resolve_payload(), payload)

    def test_frozen_without_bundled_payload_is_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sys._MEIPASS = tmp
            with self.assertRaises(installer.ToolError) as ctx:
                installer.resolve_payload()
            self.assertIn(installer.PAYLOAD_NAME, str(ctx.exception))

    def test_not_frozen_is_blocked_with_actionable_error(self) -> None:
        with self.assertRaises(installer.ToolError) as ctx:
            installer.resolve_payload()
        self.assertIn("👉 修复建议", str(ctx.exception))


class BootstrapExecutionTests(unittest.TestCase):
    def test_bootstrap_runs_in_process_not_via_sys_executable(self) -> None:
        """冻结后 sys.executable 是安装程序自己，起子进程等于递归重启安装流程。

        这里造一个从 `__file__` 派生安装目录的假 bootstrap 反证：只要真在本进程内执行，
        它就能正确写到当前解释器传入的安装目录。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / installer.SYSTEM_DIRNAME / "tools" / "bootstrap.py"
            script.parent.mkdir(parents=True)
            script.write_text(
                "from pathlib import Path\n"
                "if __name__ == '__main__':\n"
                "    ws = Path(__file__).resolve().parent.parent.parent\n"
                "    (ws / 'AGENTS.md').write_text('已初始化', encoding='utf-8')\n",
                encoding="utf-8",
            )

            res = installer.run_bootstrap(root)

            self.assertEqual(res["returncode"], 0)
            self.assertEqual((root / "AGENTS.md").read_text(encoding="utf-8"), "已初始化")

    def test_bootstrap_failure_is_reported_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / installer.SYSTEM_DIRNAME / "tools" / "bootstrap.py"
            script.parent.mkdir(parents=True)
            script.write_text("raise RuntimeError('模板缺失')\n", encoding="utf-8")

            res = installer.run_bootstrap(root)

            self.assertEqual(res["returncode"], 1)
            self.assertIn("模板缺失", res["error"])


class ExportPayloadTests(unittest.TestCase):
    def test_export_from_repo_produces_complete_payload(self) -> None:
        """端到端：真实 git archive 载荷含安装侧要求的全部必需成员。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "payload.zip"
            try:
                res = builder.export_payload(out_path=out)
            except builder.ToolError as err:
                self.skipTest(f"构建环境不具备 git 跟踪集: {err}")

            self.assertTrue(out.is_file())
            with zipfile.ZipFile(out) as archive:
                names = set(archive.namelist())
            for member in installer.REQUIRED_MEMBERS:
                self.assertIn(member, names)
            self.assertIn("tools/install_windows.py", names)
            self.assertEqual(res["payload_bytes"], out.stat().st_size)

    def test_export_rejects_unknown_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(builder.ToolError) as ctx:
                builder.export_payload(out_path=Path(tmp) / "x.zip", ref="没有这个分支")
            self.assertIn("👉 修复建议", str(ctx.exception))

    def test_payload_name_is_shared_not_duplicated(self) -> None:
        """构建侧与安装侧各写一份文件名的那天，exe 就会找不到自己的载荷。"""
        self.assertIs(builder.PAYLOAD_NAME, installer.PAYLOAD_NAME)


if __name__ == "__main__":
    unittest.main()
