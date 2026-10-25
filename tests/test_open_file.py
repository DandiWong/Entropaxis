import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import open_file
from tools.open_file import open_paths


def probe_false_verify_true(argv, **kwargs):
    """按脚本内容分流：关窗探针（含 origProc 前台还原）返回 false，窗口校验脚本返回 true。"""
    probe = "origProc" in argv[2]
    return subprocess.CompletedProcess(argv, 0, stdout="false" if probe else "true", stderr="")


class RecordingRunner:
    def __init__(self, returncodes=None):
        self.returncodes = list(returncodes or [0])
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        returncode = self.returncodes.pop(0) if self.returncodes else 0
        return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="configured opener failed")


class OpenFileTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config = self.root / "file-opener.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_config(self, associations):
        self.config.write_text(json.dumps({"associations": associations}), encoding="utf-8")

    def test_uses_configured_orca_command_for_markdown_and_images(self):
        self._write_config({
            "markdown": {
                "name": "Markdown",
                "extensions": [".md"],
                "selected_app": "Orca",
                "command": "orca file open",
                "arbitrated": True,
            },
            "image": {
                "name": "Image",
                "extensions": [".png"],
                "selected_app": "Orca",
                "command": "orca file open",
                "arbitrated": True,
            },
        })
        markdown = self.root / "notes.md"
        image = self.root / "diagram.png"
        markdown.write_text("# Notes\n", encoding="utf-8")
        image.write_bytes(b"png")
        runner = RecordingRunner([0, 0])

        results = open_paths(
            [str(markdown), str(image)],
            config_path=self.config,
            platform="darwin",
            runner=runner,
        )

        self.assertEqual(
            [call[0] for call in runner.calls],
            [["orca", "file", "open", str(markdown.resolve())],
             ["orca", "file", "open", str(image.resolve())]],
        )
        self.assertTrue(all(result["ok"] and not result["used_fallback"] for result in results))

    def test_configured_failure_falls_back_and_reports_reason(self):
        self._write_config({
            "markdown": {
                "name": "Markdown",
                "extensions": [".md"],
                "selected_app": "Orca",
                "command": "orca file open",
                "arbitrated": True,
            }
        })
        markdown = self.root / "notes.md"
        markdown.write_text("# Notes\n", encoding="utf-8")
        runner = RecordingRunner([1, 0])

        result = open_paths(
            [str(markdown)],
            config_path=self.config,
            platform="darwin",
            runner=runner,
        )[0]

        self.assertEqual(runner.calls[0][0], ["orca", "file", "open", str(markdown.resolve())])
        self.assertEqual(runner.calls[1][0], ["open", str(markdown.resolve())])
        self.assertTrue(result["ok"])
        self.assertTrue(result["used_fallback"])
        self.assertIn("configured opener failed", result["reason"])

    def test_invalid_command_falls_back_and_reports_reason(self):
        self._write_config({
            "markdown": {
                "name": "Markdown",
                "extensions": [".md"],
                "selected_app": "Broken configuration",
                "command": "\"",
            }
        })
        markdown = self.root / "notes.md"
        markdown.write_text("# Notes\n", encoding="utf-8")
        runner = RecordingRunner([0])

        result = open_paths(
            [str(markdown)],
            config_path=self.config,
            platform="darwin",
            runner=runner,
        )[0]

        self.assertEqual(runner.calls[0][0], ["open", str(markdown.resolve())])
        self.assertTrue(result["ok"])
        self.assertTrue(result["used_fallback"])
        self.assertIn("配置命令无法解析", result["reason"])


    def test_missing_config_uses_platform_default_with_visible_reason(self):
        markdown = self.root / "notes.md"
        markdown.write_text("# Notes\n", encoding="utf-8")
        runner = RecordingRunner([0])

        result = open_paths(
            [str(markdown)],
            config_path=self.config,
            platform="darwin",
            runner=runner,
        )[0]

        self.assertEqual(runner.calls[0][0], ["open", str(markdown.resolve())])
        self.assertTrue(result["ok"])
        self.assertTrue(result["used_fallback"])
        self.assertIn("配置不存在", result["reason"])
    def test_reloads_open_office_document_via_closer_hook(self):
        excel = self.root / "report.xlsx"
        excel.write_bytes(b"dummy")
        self._write_config({
            "excel": {
                "extensions": [".xlsx"],
                "selected_app": "WPS Office",
                "command": 'open -a "wpsoffice"',
            }
        })
        runner = RecordingRunner([0])
        closer_calls = []
        def fake_closer(argv, **kwargs):
            closer_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="true", stderr="")

        result = open_paths(
            [str(excel)],
            config_path=self.config,
            platform="darwin",
            runner=runner,
            closer=fake_closer,
        )[0]

        self.assertTrue(result["ok"])
        self.assertTrue(result["reloaded"])
        self.assertEqual(len(closer_calls), 2, "探针 + 窗口校验各一次")
        self.assertEqual(closer_calls[0][0], "osascript")
        self.assertIn("set frontmost", closer_calls[0][2])
        self.assertIn("origProc", closer_calls[0][2], "探针还原前台")
        self.assertIn("set frontmost", closer_calls[1][2], "校验脚本须激活应用")
        self.assertNotIn("origProc", closer_calls[1][2], "校验脚本不得还原前台")
        self.assertIs(result["verified"], True)

    def test_non_office_files_skip_closer_hook(self):
        markdown = self.root / "readme.md"
        markdown.write_text("# Readme\n", encoding="utf-8")
        self._write_config({
            "markdown": {
                "extensions": [".md"],
                "selected_app": "Orca",
                "command": "orca file open",
            }
        })
        runner = RecordingRunner([0])
        closer_calls = []
        def fake_closer(argv, **kwargs):
            closer_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="true", stderr="")

        result = open_paths(
            [str(markdown)],
            config_path=self.config,
            platform="darwin",
            runner=runner,
            closer=fake_closer,
        )[0]

        self.assertTrue(result["ok"])
        self.assertFalse(result["reloaded"])
        self.assertEqual(len(closer_calls), 0)

    def test_on_deliver_skips_markdown_and_directory(self):
        markdown = self.root / "01_调研.md"
        markdown.write_text("# 调研\n", encoding="utf-8")
        sub_dir = self.root / "capsule_dir"
        sub_dir.mkdir()
        runner = RecordingRunner([0])

        results = open_paths(
            [str(markdown), str(sub_dir)],
            config_path=self.config,
            platform="darwin",
            runner=runner,
            on_deliver=True,
        )

        self.assertTrue(all(r["ok"] and r.get("skipped") for r in results))
        self.assertEqual(len(runner.calls), 0, "交付模式下不应触发外部程序调用")

    def test_on_deliver_opens_whitelisted_office_and_images(self):
        self._write_config({
            "word": {
                "name": "Word",
                "extensions": [".docx"],
                "selected_app": "WPS",
                "command": "wps",
                "arbitrated": True,
            },
        })
        doc = self.root / "report.docx"
        doc.write_bytes(b"docx")
        runner = RecordingRunner([0])
        closer_calls = []
        def fake_closer(argv, **kwargs):
            closer_calls.append(argv)
            return probe_false_verify_true(argv, **kwargs)

        results = open_paths(
            [str(doc)],
            config_path=self.config,
            platform="darwin",
            runner=runner,
            closer=fake_closer,
            on_deliver=True,
        )

        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[0].get("skipped", False))
        self.assertEqual(len(runner.calls), 1, "白名单交付格式在交付模式下应正常打开")
        self.assertEqual(len(closer_calls), 2, "办公格式在交付时应经 closer 钩子探测热刷新并校验窗口")
        self.assertIs(results[0]["verified"], True)

    def test_on_deliver_skips_intermediate_formats(self):
        runner = RecordingRunner([0])
        for name in ("a.yaml", "a.yml", "a.json", "a.adoc", "a.asciidoc", "a.md", "a.markdown"):
            with self.subTest(name=name):
                f = self.root / name
                f.write_text("x", encoding="utf-8")
                r = open_paths([str(f)], config_path=self.config, platform="darwin",
                               runner=runner, on_deliver=True)[0]
                self.assertTrue(r["ok"] and r.get("skipped"))
        self.assertEqual(runner.calls, [])

    def test_explicit_open_still_opens_intermediate_formats(self):
        f = self.root / "a.yaml"
        f.write_text("a: 1", encoding="utf-8")
        runner = RecordingRunner([0])
        r = open_paths([str(f)], config_path=self.config, platform="darwin", runner=runner)[0]
        self.assertTrue(r["ok"])
        self.assertFalse(r.get("skipped", False))
        self.assertEqual(len(runner.calls), 1)

    def test_on_deliver_keeps_csv_and_html(self):
        for name in ("a.csv", "a.html", "a.htm"):
            with self.subTest(name=name):
                f = self.root / name
                f.write_text("x", encoding="utf-8")
                runner = RecordingRunner([0])
                r = open_paths([str(f)], config_path=self.config, platform="linux",
                               runner=runner, on_deliver=True)[0]
                self.assertFalse(r.get("skipped", False))
                self.assertEqual(len(runner.calls), 1)

    def _open_docx_with(self, closer, platform="darwin"):
        self._write_config({"word": {"extensions": [".docx"], "selected_app": "WPS Office",
                                     "command": 'open -a "wpsoffice"'}})
        doc = self.root / "report.docx"
        doc.write_bytes(b"docx")
        return open_paths([str(doc)], config_path=self.config, platform=platform,
                          runner=RecordingRunner([0]), closer=closer)[0]

    def test_verify_window_confirmed(self):
        r = self._open_docx_with(probe_false_verify_true)
        self.assertTrue(r["ok"])
        self.assertIs(r["verified"], True)

    def test_verify_window_timeout_reports_failure(self):
        def closer(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, stdout="false", stderr="")
        r = self._open_docx_with(closer)
        self.assertFalse(r["ok"])
        self.assertIs(r["verified"], False)
        self.assertIn("未见文档窗口", r["reason"])

        def raises(argv, **kw):
            if "origProc" in argv[2]:
                return subprocess.CompletedProcess(argv, 0, stdout="false", stderr="")
            raise subprocess.TimeoutExpired(argv, 25)
        r = self._open_docx_with(raises)
        self.assertFalse(r["ok"])
        self.assertIs(r["verified"], False)

    def test_verify_unknown_or_ax_denied_is_unverified(self):
        def unknown(argv, **kw):
            out = "false" if "origProc" in argv[2] else "noact"
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
        r = self._open_docx_with(unknown)
        self.assertTrue(r["ok"])
        self.assertIsNone(r["verified"])
        self.assertIn("无法激活目标应用", r["verify_note"])

        def denied(argv, **kw):
            if "origProc" in argv[2]:
                return subprocess.CompletedProcess(argv, 0, stdout="false", stderr="")
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="execution error: (-25211)")
        r = self._open_docx_with(denied)
        self.assertTrue(r["ok"])
        self.assertIsNone(r["verified"])
        self.assertIn("辅助功能权限", r["verify_note"])

    def test_verify_skipped_off_macos(self):
        calls = []

        def closer(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="true", stderr="")
        r = self._open_docx_with(closer, platform="linux")
        self.assertTrue(r["ok"])
        self.assertIsNone(r["verified"])
        self.assertIn("本平台仅校验命令退出码", r["verify_note"])
        self.assertEqual(calls, [])

    def test_probe_restores_front_unconditionally(self):
        scripts = []

        def closer(argv, **kw):
            scripts.append(argv[2])
            return probe_false_verify_true(argv, **kw)
        self._open_docx_with(closer)
        probe = scripts[0]
        self.assertEqual(len(scripts), 2, "探针 1 次 + 校验 1 次，探针在前")
        self.assertIn("set frontmost", probe)
        self.assertIn("set frontmost", scripts[1])
        self.assertNotIn("origProc", scripts[1])
        self.assertNotIn("if not closedAny", probe)
        self.assertGreater(probe.rindex("set frontmost of origProc"), probe.rindex("end repeat"))

    def test_non_desktop_office_opener_skips_closer(self):
        """若已配置使用 Orca 等非桌面应用打开，无需触发 AX 窗口关闭探针。"""
        self._write_config({
            "pdf": {
                "name": "PDF",
                "extensions": [".pdf"],
                "selected_app": "Orca",
                "command": "orca file open",
                "arbitrated": True,
            },
        })
        pdf = self.root / "doc.pdf"
        pdf.write_bytes(b"%PDF")
        runner = RecordingRunner([0])
        closer_calls = []
        def fake_closer(argv, **kwargs):
            closer_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="false", stderr="")

        results = open_paths(
            [str(pdf)],
            config_path=self.config,
            platform="darwin",
            runner=runner,
            closer=fake_closer,
            on_deliver=True,
        )
        self.assertTrue(results[0]["ok"])
        self.assertEqual(len(closer_calls), 0, "非桌面应用配置应直接跳过 AX 窗口探针")
if __name__ == "__main__":
    unittest.main()


class DirectoryAssociationTests(unittest.TestCase):
    """目录不再永久降级（E）。

    目录此前一律无关联项，每次打开都记一次「降级」；而降级按《文件交付》§4.5 要逐项
    告知用户——把天天发生且本来就正确的路径记成异常，会训练人忽略降级提示。
    """

    CONFIG = {"associations": {"directory": {"name": "目录", "extensions": [], "command": "open"}}}

    def test_directory_uses_configured_opener(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            calls = []

            def runner(argv, **kw):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, "", "")

            res = open_file._open_path(td, self.CONFIG, "", "darwin", runner, closer=lambda *a, **k: None)
            self.assertTrue(res["ok"])
            self.assertFalse(res["used_fallback"], f"目录已配置打开器就不该记降级: {res['reason']}")
            self.assertEqual(calls[0][0], "open")

    def test_directory_without_config_still_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            res = open_file._open_path(
                td, {"associations": {}}, "", "darwin",
                lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""), closer=lambda *a, **k: None)
            self.assertTrue(res["used_fallback"])

    def test_url_opens_in_default_browser(self) -> None:
        runner = RecordingRunner()
        no_tab = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "false", "")
        results = open_paths(["https://example.com/orders"], config_path=Path("/nonexistent.json"),
                             platform="darwin", runner=runner, closer=no_tab)
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[0]["reloaded"])
        self.assertEqual(runner.calls[0][0], ["open", "https://example.com/orders"])

    def test_url_reloads_existing_tab_instead_of_new_one(self) -> None:
        runner = RecordingRunner()
        scripts = []

        def tab_found(argv, **kw):
            scripts.append(argv[2])
            return subprocess.CompletedProcess(argv, 0, "true", "")

        res = open_paths(["http://example.test:3000/"], config_path=Path("/nonexistent.json"),
                         platform="darwin", runner=runner, closer=tab_found)[0]
        self.assertTrue(res["ok"] and res["reloaded"])
        self.assertEqual(res["selected_app"], "Google Chrome")
        self.assertEqual(runner.calls, [], "命中已有标签页时不得再新开")
        self.assertIn('"http://example.test:3000"', scripts[0], "尾斜杠应归一后匹配")

    def test_html_file_reloads_existing_tab(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            page = Path(td) / "原型.html"
            page.write_text("<p>v2</p>", encoding="utf-8")
            runner = RecordingRunner()
            scripts = []

            def safari_only(argv, **kw):
                scripts.append(argv[2])
                hit = "Safari" in argv[2]
                return subprocess.CompletedProcess(argv, 0, "true" if hit else "false", "")

            res = open_paths([str(page)], config_path=Path("/nonexistent.json"), platform="darwin",
                             runner=runner, closer=safari_only, on_deliver=True)[0]
            self.assertTrue(res["reloaded"])
            self.assertEqual(res["selected_app"], "Safari")
            self.assertEqual(runner.calls, [])
            self.assertIn(page.resolve().as_uri(), scripts[0])

    def test_reload_probe_skipped_off_macos(self) -> None:
        self.assertEqual(open_file._reload_browser_tab("http://x", "linux", closer=None), "")


class PlatformPartitionTests(unittest.TestCase):
    """platforms.<sys.platform>.associations 分区读取（darwin/win32/linux 三平台模拟）。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config = self.root / "file-opener.json"
        self.addCleanup(self.temp_dir.cleanup)

    def _write_platform_config(self, platform, associations):
        self.config.write_text(
            json.dumps({"platforms": {platform: {"associations": associations}}}), encoding="utf-8"
        )

    def test_windows_reads_win32_partition_command(self):
        self._write_platform_config("win32", {
            "word": {
                "name": "Word文档",
                "extensions": [".docx", ".doc"],
                "selected_app": "Microsoft Word",
                "command": "start winword",
            },
        })
        doc = self.root / "report.docx"
        doc.write_bytes(b"docx")
        runner = RecordingRunner([0])

        result = open_paths([str(doc)], config_path=self.config, platform="win32", runner=runner)[0]

        self.assertEqual(runner.calls[0][0], ["cmd", "/c", "start", "winword", str(doc.resolve())])
        self.assertTrue(result["ok"])
        self.assertFalse(result["used_fallback"])

    def test_missing_current_platform_partition_falls_back_with_bootstrap_hint(self):
        self._write_platform_config("darwin", {
            "word": {
                "name": "Word文档",
                "extensions": [".docx", ".doc"],
                "selected_app": "Microsoft Word",
                "command": 'open -a "Microsoft Word"',
            },
        })
        doc = self.root / "report.docx"
        doc.write_bytes(b"docx")
        runner = RecordingRunner([0])

        result = open_paths([str(doc)], config_path=self.config, platform="win32", runner=runner)[0]

        self.assertEqual(runner.calls[0][0], ["cmd", "/c", "start", "", str(doc.resolve())])
        self.assertTrue(result["ok"])
        self.assertTrue(result["used_fallback"])
        self.assertIn("本平台未配置打开器，运行 bootstrap.py 生成", result["reason"])

    def test_legacy_config_from_other_platform_is_not_applied(self):
        """旧版单平台配置：platform 字段与他平台不符时不得把他平台命令当本平台配置执行。"""
        self.config.write_text(json.dumps({
            "version": "1.0.0",
            "platform": "darwin",
            "associations": {
                "word": {
                    "name": "Word文档",
                    "extensions": [".docx", ".doc"],
                    "selected_app": "Microsoft Word",
                    "command": 'open -a "Microsoft Word"',
                },
            },
        }), encoding="utf-8")
        doc = self.root / "report.docx"
        doc.write_bytes(b"docx")
        runner = RecordingRunner([0])

        result = open_paths([str(doc)], config_path=self.config, platform="linux", runner=runner)[0]

        self.assertEqual(runner.calls[0][0], ["xdg-open", str(doc.resolve())])
        self.assertTrue(result["used_fallback"])
        self.assertIn("本平台未配置打开器，运行 bootstrap.py 生成", result["reason"])

    def test_legacy_config_without_platform_field_applies_on_current(self):
        self.config.write_text(json.dumps({"associations": {
            "word": {
                "name": "Word文档",
                "extensions": [".docx", ".doc"],
                "selected_app": "Microsoft Word",
                "command": 'open -a "Microsoft Word"',
            },
        }}), encoding="utf-8")
        doc = self.root / "report.docx"
        doc.write_bytes(b"docx")
        runner = RecordingRunner([0])

        result = open_paths([str(doc)], config_path=self.config, platform="darwin", runner=runner,
                            closer=probe_false_verify_true)[0]

        self.assertEqual(runner.calls[0][0][:3], ["open", "-a", "Microsoft Word"])
        self.assertTrue(result["ok"])
        self.assertFalse(result["used_fallback"])

    def test_default_opener_argv_per_platform(self):
        """无配置时各平台走各自的系统默认命令：darwin→open、win32→cmd /c start ""、linux→xdg-open。"""
        note = self.root / "plain.txt"
        note.write_text("x", encoding="utf-8")
        target = str(note.resolve())
        expectations = {
            "darwin": ["open", target],
            "win32": ["cmd", "/c", "start", "", target],
            "linux": ["xdg-open", target],
        }
        for platform, expected in expectations.items():
            with self.subTest(platform=platform):
                runner = RecordingRunner([0])
                result = open_paths(
                    [str(note)], config_path=Path("/nonexistent.json"), platform=platform, runner=runner
                )[0]
                self.assertEqual(runner.calls[0][0], expected)
                self.assertTrue(result["used_fallback"])

    def test_windows_directory_default_opener_is_explorer(self):
        sub_dir = self.root / "capsule_dir"
        sub_dir.mkdir()
        runner = RecordingRunner([0])

        result = open_paths(
            [str(sub_dir)], config_path=Path("/nonexistent.json"), platform="win32", runner=runner
        )[0]

        self.assertEqual(runner.calls[0][0], ["explorer", str(sub_dir.resolve())])
        self.assertTrue(result["used_fallback"])
