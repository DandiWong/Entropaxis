import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import open_file
from tools.open_file import open_paths


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
        self.assertEqual(len(closer_calls), 1)
        self.assertEqual(closer_calls[0][0], "osascript")

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
