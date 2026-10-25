import json
import re
import shutil
import subprocess
import tempfile
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools import open_file
from tools.open_file import open_paths


RS, US = chr(30), chr(31)


def window_report(status="ok", titles=()):
    """模拟 AppleScript 回传：<状态>RS<标题>US<标题>US…（末尾带 osascript 的换行）。"""
    return status + RS + "".join(t + US for t in titles) + "\n"


def window_closer(status="ok", titles=(), probe="false"):
    """按脚本内容分流：关窗探针（含 origProc）返回 probe（默认 false=没关窗），窗口校验脚本回传窗口标题报告，由 Python 侧判定命中。"""
    def closer(argv, **kwargs):
        out = probe if "origProc" in argv[2] else window_report(status, titles)
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
    return closer


# report.docx / report.xlsx 的窗口都在：命中判定走真实 Python 谓词
probe_false_verify_true = window_closer("ok", ["report.docx - WPS Office", "report.xlsx - WPS Office"])


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
            return window_closer("ok", ["report.xlsx - WPS Office"], probe="true")(argv, **kwargs)

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
        self.assertIn("class of ws is not list", closer_calls[1][2], "单窗口时 AppleScript 返回字符串，须包成列表（真机回归）")
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
        with mock.patch.object(open_file.time, "sleep"), mock.patch.object(open_file, "VERIFY_TIMEOUT_S", 0):
            r = self._open_docx_with(window_closer("ok", ["other.docx - WPS Office"]))
        self.assertFalse(r["ok"])
        self.assertIs(r["verified"], False)
        self.assertIn("未见文档窗口", r["reason"])

        def raises(argv, **kw):
            if "origProc" in argv[2]:
                return subprocess.CompletedProcess(argv, 0, stdout="false", stderr="")
            raise subprocess.TimeoutExpired(argv, 10)
        r = self._open_docx_with(raises)
        self.assertFalse(r["ok"])
        self.assertIs(r["verified"], False)

    def test_verify_unknown_or_ax_denied_is_unverified(self):
        with mock.patch.object(open_file.time, "sleep"), mock.patch.object(open_file, "VERIFY_TIMEOUT_S", 0):
            r = self._open_docx_with(window_closer("noact"))
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


class WindowMatchTests(unittest.TestCase):
    """窗口命中判定在 Python 侧：closer 只回传窗口标题，是否命中由真实谓词决定（匹配被破坏时这些用例必须失败）。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.doc = Path(tmp.name) / "报告 v1.docx"
        self.doc.write_bytes(b"docx")
        for p in (mock.patch.object(open_file.time, "sleep"), mock.patch.object(open_file, "VERIFY_TIMEOUT_S", 0)):
            p.start()
            self.addCleanup(p.stop)

    def verify(self, status, titles, stdout=None):
        def closer(argv, **kw):
            out = stdout if stdout is not None else window_report(status, titles)
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
        return open_file._verify_window_open(self.doc, "darwin", closer, "", "WPS Office")

    def test_title_containing_file_name_hits(self):
        self.assertEqual(self.verify("ok", ["报告 v1.docx - WPS Office"]), (True, ""))
        self.assertIs(self.verify("ok", ["a.docx", "报告 V1.DOCX"])[0], True, "大小写不敏感，多窗口任一命中")

    def test_title_without_file_name_does_not_hit(self):
        self.assertIs(self.verify("ok", ["报告 v2.docx - WPS Office", "文档1"])[0], False)
        self.assertIs(self.verify("ok", ["报告 v1"])[0], False, "仅含部分名不算命中")

    def test_empty_window_list_or_app_missing_does_not_hit(self):
        self.assertIs(self.verify("ok", [])[0], False)
        self.assertIs(self.verify("noproc", [])[0], False, "应用未启动：不命中")
        self.assertIsNone(self.verify("noact", [])[0], "进程在但无法激活：无法校验，不误判命中")

    def test_title_parsing_is_robust(self):
        multiline = "x.docx\n报告 v1.docx\n- WPS"
        self.assertIs(self.verify("ok", [multiline, "other"])[0], True, "标题内换行原样保留，不被切断")
        self.assertIs(self.verify("ok", ['a"b', "c,d;e|f", "报告 v2.docx"])[0], False, "特殊字符标题不命中也不崩溃")
        self.assertIs(self.verify("ok", ["", "  ", ""])[0], False, "空标题被丢弃")
        for garbage in ("", "\n", "true\n", "false", "ok", "weird" + RS + "报告 v1.docx" + US, RS + "报告 v1.docx"):
            with self.subTest(garbage=garbage):
                self.assertIsNone(self.verify(None, None, stdout=garbage)[0], "格式异常按无法校验处理，绝不判命中")


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("osascript"), "需要 macOS osascript（纯 AppleScript 计算，不控制其它应用）")
class AppleScriptEnumerationTests(unittest.TestCase):
    """真实 osascript 执行生产的标题枚举/编码片段，再交给生产解析与命中判定；去掉标量归一化语句，标量用例必须失败。"""

    def run_as(self, ws_expr):
        script = "\n".join([
            open_file._TITLES_PRELUDE,
            open_file._enum_titles_as(ws_expr),
            'return "ok" & RS & titles',
        ])
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(res.returncode, 0, res.stderr)
        return open_file._parse_window_report(res.stdout)

    def test_scalar_single_title_hits(self):
        status, titles = self.run_as('"a.docx - WPS Office"')  # 单窗口时 AX 返回字符串而非列表
        self.assertEqual((status, titles), ("ok", ["a.docx - WPS Office"]))
        self.assertTrue(open_file._title_hits(titles, "a.docx"))

    def test_list_of_titles_hits_any(self):
        status, titles = self.run_as('{"b.docx", "a.docx - WPS Office"}')
        self.assertEqual((status, titles), ("ok", ["b.docx", "a.docx - WPS Office"]))
        self.assertTrue(open_file._title_hits(titles, "a.docx"))
        self.assertFalse(open_file._title_hits(titles, "c.docx"))

    def test_empty_title_set_does_not_hit(self):
        status, titles = self.run_as("{}")
        self.assertEqual((status, titles), ("ok", []))
        self.assertFalse(open_file._title_hits(titles, "a.docx"))


class CliWordingTests(unittest.TestCase):
    """CLI 文本：只有 verified=True 才出现完成式「已…打开」；其余成功一律「发出打开命令，未确认窗口」。"""

    DONE = re.compile(r"已[^，；（\n]*?打开(?!命令)")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for p in (mock.patch.object(open_file.time, "sleep"), mock.patch.object(open_file, "VERIFY_TIMEOUT_S", 0)):
            p.start()
            self.addCleanup(p.stop)

    def cli(self, paths, *, on_deliver=False, **kw):
        kw.setdefault("config_path", self.root / "missing.json")
        kw.setdefault("platform", "darwin")
        kw.setdefault("runner", RecordingRunner([0]))
        results = open_paths([str(p) for p in paths], on_deliver=on_deliver, **kw)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["open_file.py", *map(str, paths)]), \
                mock.patch.object(open_file, "open_paths", return_value=results), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = open_file.main()
        return results[0], rc, out.getvalue(), err.getvalue()

    def assert_unverified_wording(self, result, rc, out):
        self.assertIsNone(result["verified"])
        self.assertEqual(rc, 0)
        self.assertIn("发出打开命令", out)
        self.assertIn("未确认窗口", out)
        self.assertNotIn("已确认文档窗口", out)
        self.assertIsNone(self.DONE.search(out), out)

    def _file(self, name):
        p = self.root / name
        p.write_bytes(b"x")
        return p

    def test_fallback_unverified_has_no_done_claim(self):
        r, rc, out, _ = self.cli([self._file("a.yaml")])
        self.assertTrue(r["used_fallback"])
        self.assert_unverified_wording(r, rc, out)
        self.assertIn("配置不存在", out, "保留降级原因")
        self.assertIn("系统默认程序", out)

    def test_fallback_office_unverified_has_no_done_claim(self):
        r, rc, out, _ = self.cli([self._file("a.docx")], closer=window_closer("noact"))
        self.assertTrue(r["used_fallback"])
        self.assert_unverified_wording(r, rc, out)

    def test_configured_unverified_has_no_done_claim(self):
        cfg = self.root / "cfg.json"
        cfg.write_text(json.dumps({"associations": {"md": {
            "extensions": [".md"], "selected_app": "Orca", "command": "orca file open"}}}))
        r, rc, out, _ = self.cli([self._file("a.md")], config_path=cfg)
        self.assertFalse(r["used_fallback"])
        self.assert_unverified_wording(r, rc, out)

    def test_directory_and_url_unverified_have_no_done_claim(self):
        d = self.root / "dir"
        d.mkdir()
        r, rc, out, _ = self.cli([d])
        self.assert_unverified_wording(r, rc, out)
        no_tab = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "false", "")
        r, rc, out, _ = self.cli(["https://example.com/x"], closer=no_tab)
        self.assert_unverified_wording(r, rc, out)

    def test_skipped_has_no_done_claim(self):
        r, rc, out, _ = self.cli([self._file("a.yaml")], on_deliver=True)
        self.assertTrue(r["skipped"])
        self.assertIn("[SKIP]", out)
        self.assertIsNone(self.DONE.search(out), out)

    def test_confirmed_must_say_confirmed(self):
        titles = ["a.docx - WPS Office"]
        r, rc, out, _ = self.cli([self._file("a.docx")], closer=window_closer("ok", titles))  # 降级路径
        self.assertTrue(r["used_fallback"])
        self.assertIs(r["verified"], True)
        self.assertEqual(rc, 0)
        self.assertIn("已确认文档窗口", out)

        cfg = self.root / "cfg.json"
        cfg.write_text(json.dumps({"associations": {"w": {
            "extensions": [".docx"], "selected_app": "WPS Office", "command": 'open -a "wpsoffice"'}}}))
        r, rc, out, _ = self.cli([self.root / "a.docx"], config_path=cfg, closer=window_closer("ok", titles))
        self.assertFalse(r["used_fallback"])
        self.assertIn("已使用 WPS Office 打开", out)
        self.assertIn("已确认文档窗口", out)

        reload_ok = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "true", "")
        r, rc, out, _ = self.cli([self._file("p.html")], closer=reload_ok)
        self.assertIs(r["verified"], True)
        self.assertIn("已确认文档窗口", out)

    def test_not_confirmed_keeps_failure_semantics(self):
        r, rc, out, err = self.cli([self._file("a.docx")], closer=window_closer("ok", ["other.docx"]))
        self.assertIs(r["verified"], False)
        self.assertEqual(rc, 1)
        self.assertIn("未确认打开", err)
        self.assertEqual(out, "")
