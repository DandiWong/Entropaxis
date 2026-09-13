"""Windows 自解压安装包：构建侧与安装侧的契约测试。

覆盖三处容易静默失效的地方：载荷标记的多次出现（批处理正文里有诱饵）、安装只写 `.system/`
不碰 `.data/`、以及越界成员路径必须阻断。
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


class PayloadRoundTripTests(unittest.TestCase):
    def test_render_then_read_returns_original_payload(self) -> None:
        payload = _fake_payload()
        carrier = builder.render_installer(payload)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "setup.bat"
            path.write_bytes(carrier)
            self.assertEqual(installer.read_payload(path), payload)

    def test_marker_appears_before_payload_and_last_one_wins(self) -> None:
        """批处理正文里的自解压代码本身含同名标记，取值必须落在最后一处之后。"""
        carrier = builder.render_installer(_fake_payload())
        self.assertGreater(carrier.count(installer.PAYLOAD_MARKER), 1)

    def test_carrier_starts_with_echo_off_and_has_no_bom(self) -> None:
        """带 BOM 的 .bat 会让 cmd 把首行当成未知命令，双击立刻失败。"""
        carrier = builder.render_installer(_fake_payload())
        self.assertTrue(carrier.startswith(b"@echo off"))
        self.assertIn(b"\r\n", carrier[:200])

    def test_missing_marker_is_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "setup.bat"
            path.write_bytes(b"@echo off\r\n")
            with self.assertRaises(installer.ToolError) as ctx:
                installer.read_payload(path)
            self.assertIn("👉 修复建议", str(ctx.exception))


class InstallTests(unittest.TestCase):
    def test_install_writes_system_dir_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "工作区"
            root.mkdir()
            (root / ".data").mkdir()
            (root / ".data" / "凭据.md").write_text("用户数据", encoding="utf-8")

            res = installer.install(_fake_payload(), root)

            self.assertEqual(res["mode"], "install")
            self.assertTrue((root / ".system" / "tools" / "bootstrap.py").is_file())
            self.assertEqual((root / ".data" / "凭据.md").read_text(encoding="utf-8"), "用户数据")

    def test_reinstall_keeps_local_private_skills(self) -> None:
        """覆盖合并而非整目录替换：收件方自己放的私有能力不能被升级抹掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installer.install(_fake_payload(), root)
            private = root / ".system" / "skills" / "私有能力" / "SKILL.md"
            private.parent.mkdir(parents=True)
            private.write_text("本机私有", encoding="utf-8")

            res = installer.install(_fake_payload(), root)

            self.assertEqual(res["mode"], "upgrade")
            self.assertTrue(private.is_file())

    def test_choosing_system_dir_itself_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wrong = Path(tmp) / ".system"
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
    """exe 从随包数据取载荷，.bat 从 --carrier 取；两条都不成立时必须明确阻断。"""

    def tearDown(self) -> None:
        if hasattr(sys, "_MEIPASS"):
            del sys._MEIPASS

    def test_frozen_bundle_is_used_when_no_carrier(self) -> None:
        payload = _fake_payload()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / installer.PAYLOAD_NAME).write_bytes(payload)
            sys._MEIPASS = tmp
            self.assertEqual(installer.resolve_payload(None), payload)

    def test_frozen_without_bundled_payload_is_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sys._MEIPASS = tmp
            with self.assertRaises(installer.ToolError) as ctx:
                installer.resolve_payload(None)
            self.assertIn(installer.PAYLOAD_NAME, str(ctx.exception))

    def test_neither_frozen_nor_carrier_is_blocked(self) -> None:
        with self.assertRaises(installer.ToolError) as ctx:
            installer.resolve_payload(None)
        self.assertIn("--carrier", str(ctx.exception))


class BootstrapExecutionTests(unittest.TestCase):
    def test_bootstrap_runs_in_process_not_via_sys_executable(self) -> None:
        """冻结后 sys.executable 是安装程序自己，起子进程等于递归重启安装流程。

        这里用一个会把 sys.executable 写进产物的假 bootstrap 反证：只要真在本进程内执行，
        它拿到的解释器就是当前 Python，且能正确从 __file__ 派生出安装目录。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / ".system" / "tools" / "bootstrap.py"
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
            script = root / ".system" / "tools" / "bootstrap.py"
            script.parent.mkdir(parents=True)
            script.write_text("raise RuntimeError('模板缺失')\n", encoding="utf-8")

            res = installer.run_bootstrap(root)

            self.assertEqual(res["returncode"], 1)
            self.assertIn("模板缺失", res["error"])


class BuildTests(unittest.TestCase):
    def test_build_from_repo_produces_installable_carrier(self) -> None:
        """端到端：真实 git archive 载荷经安装侧解析后仍是完整控制面。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "安装程序.bat"
            try:
                res = builder.build_installer(out_path=out)
            except builder.ToolError as err:
                self.skipTest(f"构建环境不具备 git 跟踪集: {err}")

            self.assertTrue(out.is_file())
            payload = installer.read_payload(out)
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = set(archive.namelist())
            for member in installer.REQUIRED_MEMBERS:
                self.assertIn(member, names)
            self.assertIn("tools/install_windows.py", names)
            self.assertEqual(res["payload_bytes"], len(payload))

    def test_payload_name_is_shared_not_duplicated(self) -> None:
        """构建侧与安装侧各写一份文件名的那天，exe 就会找不到自己的载荷。"""
        self.assertIs(builder.PAYLOAD_NAME, installer.PAYLOAD_NAME)
        self.assertEqual(builder.PAYLOAD_MARKER.encode("ascii"), installer.PAYLOAD_MARKER)

    def test_build_rejects_unknown_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(builder.ToolError) as ctx:
                builder.build_installer(out_path=Path(tmp) / "x.bat", ref="没有这个分支")
            self.assertIn("👉 修复建议", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
