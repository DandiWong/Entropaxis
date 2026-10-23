import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import TestCase, mock

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))

from tools import bootstrap, role_preferences
from tools.bootstrap import (
    detect_host_apps,
    detect_role_preferences,
    init_file_opener,
    link_skills,
    sync_entrypoints,
)

# ponytail: sync_entrypoints() 的路径由自身 __file__ 派生，无法用 monkeypatch 隔离到临时目录；
# 直接对真实工作区跑，并用 finally 恢复，覆盖幂等性与漂移自愈两条核心路径。


class BootstrapSyncTests(TestCase):
    def test_sync_is_idempotent(self) -> None:
        self.assertTrue(sync_entrypoints(verbose=False))
        self.assertTrue(sync_entrypoints(verbose=False))
        for filename in ("AGENTS.md", "CLAUDE.md"):
            source = SYSTEM_ROOT / "entrypoints" / filename
            target = SYSTEM_ROOT.parent / filename
            self.assertEqual(target.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))

    def test_sync_self_heals_drifted_target(self) -> None:
        target = SYSTEM_ROOT.parent / "AGENTS.md"
        source = SYSTEM_ROOT / "entrypoints" / "AGENTS.md"
        original = target.read_text(encoding="utf-8")
        try:
            target.write_text("drifted content", encoding="utf-8")
            self.assertTrue(sync_entrypoints(verbose=False))
            self.assertEqual(target.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
        finally:
            target.write_text(original, encoding="utf-8")


class LinkSkillsTests(TestCase):
    def test_links_skills_keeps_foreign_and_prunes_dangling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            system = Path(tmp) / ".entropaxis"
            for name in ("alpha", "beta"):
                (system / "skills" / name).mkdir(parents=True)
                (system / "skills" / name / "SKILL.md").write_text("x", encoding="utf-8")
            claude = Path(tmp) / ".claude" / "skills"
            (claude / "beta").mkdir(parents=True)  # 用户另装的同名 Skill
            (claude / "gone").symlink_to("../../.entropaxis/skills/gone")  # 已退役 Skill 的残链

            self.assertTrue(link_skills(verbose=False, system_dir=system))
            self.assertTrue(link_skills(verbose=False, system_dir=system))  # 幂等

            self.assertTrue((claude / "alpha" / "SKILL.md").is_file())
            self.assertTrue((Path(tmp) / ".agents" / "skills" / "beta" / "SKILL.md").is_file())
            self.assertFalse((claude / "beta").is_symlink())
            self.assertFalse((claude / "gone").is_symlink())



class _OpenerSandbox(TestCase):
    """临时 .entropaxis 沙盒：拷入真实模板，配置落 data/templates/，绝不触碰工作区实例。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="file-opener-"))
        self.system_dir = self.tmp / ".entropaxis"
        (self.system_dir / "templates" / "instance").mkdir(parents=True)
        shutil.copy(
            SYSTEM_ROOT / "templates" / "instance" / "file-opener.template.json",
            self.system_dir / "templates" / "instance" / "file-opener.template.json",
        )
        self.data_dir = self.system_dir / "data"
        (self.data_dir / "templates").mkdir(parents=True)
        # 模板渲染产物落 data/templates/，与源模板同名，路径即来源指针
        self.config_path = self.data_dir / "templates" / "file-opener.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _init(self, **kwargs):
        """固定探测结果为空集，保证命令选择只由平台分支决定。"""
        with mock.patch.object(bootstrap, "detect_host_apps", return_value=set()):
            return init_file_opener(verbose=False, system_dir=self.system_dir, **kwargs)


class FileOpenerMergeTests(_OpenerSandbox):
    """merge-preserve 策略（按平台分区）：人工仲裁项持久保留；全量重扫仅显式触发（G-13）。"""

    def _partition_config(self, platform: str, associations: dict) -> dict:
        return {"version": "1.0.0", "platforms": {platform: {"associations": associations}}}

    def _arbitrated_markdown(self) -> dict:
        return {
            "name": "Markdown与标记文档",
            "extensions": [".md"],
            "selected_app": "Orca (orca file open)",
            "command": "orca file open",
            "available_candidates": ["Orca (orca file open)"],
        }

    def test_default_run_preserves_arbitration_and_fills_missing(self) -> None:
        self.config_path.write_text(
            json.dumps(self._partition_config("darwin", {"markdown": self._arbitrated_markdown()}), ensure_ascii=False),
            encoding="utf-8",
        )
        cfg = self._init(platform="darwin")
        darwin = cfg["platforms"]["darwin"]["associations"]
        self.assertEqual(darwin["markdown"]["command"], "orca file open")
        self.assertIn("word", darwin)
        on_disk = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["platforms"]["darwin"]["associations"]["markdown"]["command"], "orca file open")
        self.assertIn("word", on_disk["platforms"]["darwin"]["associations"])

    def test_force_rescan_rebuilds_from_template(self) -> None:
        self.config_path.write_text(
            json.dumps(self._partition_config("darwin", {"markdown": self._arbitrated_markdown()}), ensure_ascii=False),
            encoding="utf-8",
        )
        cfg = self._init(platform="darwin", force_rescan=True)
        self.assertNotEqual(cfg["platforms"]["darwin"]["associations"]["markdown"]["command"], "orca file open")

    def test_corrupt_config_self_heals(self) -> None:
        self.config_path.write_text("{not-json", encoding="utf-8")
        cfg = self._init(platform="win32")
        self.assertIn("markdown", cfg["platforms"]["win32"]["associations"])
        json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_rerun_without_change_keeps_file_stable(self) -> None:
        self.config_path.write_text(json.dumps({
            "version": "1.0.0",
            "platform": "darwin",
            "associations": {"markdown": self._arbitrated_markdown()},
        }, ensure_ascii=False), encoding="utf-8")
        first = self._init(platform="darwin")
        stamp = self.config_path.stat().st_mtime_ns
        second = self._init(platform="darwin")
        self.assertEqual(first, second)
        self.assertEqual(self.config_path.stat().st_mtime_ns, stamp)


class FileOpenerPlatformPartitionTests(_OpenerSandbox):
    """三平台适配（体检第 36 项）：darwin/win32/linux 分区隔离、旧格式迁移与他平台值不误判仲裁。"""

    def _legacy_darwin_config(self) -> dict:
        """旧版 bootstrap 在 macOS 上的产物形态：顶层 associations + platform 字段。"""
        return {
            "version": "1.0.0",
            "platform": "darwin",
            "associations": {
                "word": {
                    "name": "Word文档",
                    "extensions": [".docx", ".doc"],
                    "selected_app": "Microsoft Word",
                    "command": 'open -a "Microsoft Word"',
                    "available_candidates": ["Microsoft Word"],
                },
                "markdown": {
                    "name": "Markdown与标记文档",
                    "extensions": [".md", ".markdown", ".asciidoc", ".adoc"],
                    "selected_app": "Typora",
                    "command": 'open -a "Typora"',
                    "available_candidates": ["Typora"],
                },
            },
        }

    def test_macos_legacy_config_migrates_on_windows_bootstrap(self) -> None:
        """必测场景：macOS 旧格式随工作区搬到 Windows 后 bootstrap——win32 分区按 Windows
        命令新生成且不打 arbitrated，darwin 分区整体原样归档，值一个都不改写。"""
        self.config_path.write_text(json.dumps(self._legacy_darwin_config(), ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(bootstrap, "detect_host_apps", return_value={"winword", "wps"}):
            cfg = init_file_opener(verbose=False, system_dir=self.system_dir, platform="win32")
        win32 = cfg["platforms"]["win32"]["associations"]
        self.assertEqual(win32["word"]["command"], "start winword")
        self.assertFalse(any(info.get("arbitrated") for info in win32.values()))
        self.assertEqual(cfg["platforms"]["darwin"]["associations"], self._legacy_darwin_config()["associations"])
        self.assertNotIn("associations", cfg)  # 顶层旧字段已迁移，平台信息内化为分区键
        self.assertNotIn("platform", cfg)
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8")), cfg)

    def test_fresh_generation_uses_platform_specific_fallback_commands(self) -> None:
        for platform, fallback in (("darwin", "open"), ("win32", 'start ""'), ("linux", "xdg-open")):
            with self.subTest(platform=platform):
                self.config_path.unlink(missing_ok=True)
                cfg = self._init(platform=platform)
                self.assertEqual(set(cfg["platforms"]), {platform})
                word = cfg["platforms"][platform]["associations"]["word"]
                self.assertEqual(word["command"], fallback)
                self.assertEqual(word["selected_app"], "系统默认关联程序")

    def test_linux_generation_picks_linux_candidates(self) -> None:
        with mock.patch.object(bootstrap, "detect_host_apps", return_value={"wps"}):
            cfg = init_file_opener(verbose=False, system_dir=self.system_dir, platform="linux")
        word = cfg["platforms"]["linux"]["associations"]["word"]
        self.assertEqual(word["command"], "wps")  # 模板 cmd_linux，而非他平台命令
        # 模板把 Word 的 cmd_linux 也声明为 wps，首个命中即选中，应用名断言无增量价值

    def test_windows_exe_names_match_microsoft_office_candidates(self) -> None:
        """真实缺陷回归：win32 探测到的是 exe stem（WINWORD.EXE→winword、POWERPNT.EXE→powerpnt），
        只按候选名子串匹配永远命中不了，Word/PowerPoint 已安装却回退系统默认。"""
        with mock.patch.object(bootstrap, "detect_host_apps", return_value={"winword", "powerpnt"}):
            cfg = init_file_opener(verbose=False, system_dir=self.system_dir, platform="win32")
        assoc = cfg["platforms"]["win32"]["associations"]
        self.assertEqual(assoc["word"]["command"], "start winword")
        self.assertEqual(assoc["ppt"]["command"], "start powerpnt")

    def test_arbitrated_only_judged_within_current_platform(self) -> None:
        """他平台分区里的差异化命令不得被打 arbitrated，也不得被回填覆盖。"""
        manual_win32_word = {
            "name": "Word文档",
            "extensions": [".docx", ".doc"],
            "selected_app": "自定写作器",
            "command": "start customwriter",
        }
        self.config_path.write_text(json.dumps({
            "version": "1.0.0",
            "platforms": {"win32": {"associations": {"word": manual_win32_word}}},
        }, ensure_ascii=False), encoding="utf-8")
        cfg = self._init(platform="darwin")
        self.assertEqual(cfg["platforms"]["win32"]["associations"]["word"], manual_win32_word)
        self.assertIn("word", cfg["platforms"]["darwin"]["associations"])  # 当前平台照常回填
        self.assertNotIn("excel", cfg["platforms"]["win32"]["associations"])  # 他平台分区不动

    def test_legacy_config_without_platform_field_archives_under_current(self) -> None:
        self.config_path.write_text(json.dumps({
            "version": "1.0.0",
            "associations": {"markdown": {
                "name": "Markdown与标记文档", "extensions": [".md"],
                "selected_app": "Orca", "command": "orca file open",
            }},
        }, ensure_ascii=False), encoding="utf-8")
        cfg = self._init(platform="linux")
        self.assertEqual(set(cfg["platforms"]), {"linux"})
        self.assertEqual(cfg["platforms"]["linux"]["associations"]["markdown"]["command"], "orca file open")

    def test_force_rescan_rebuilds_only_current_platform_partition(self) -> None:
        win32_partition = {"associations": {"word": {
            "name": "Word文档", "extensions": [".docx", ".doc"],
            "selected_app": "自定写作器", "command": "start customwriter", "arbitrated": True,
        }}}
        self.config_path.write_text(json.dumps({
            "version": "1.0.0",
            "platforms": {
                "darwin": {"associations": {"markdown": {
                    "name": "Markdown与标记文档", "extensions": [".md"],
                    "selected_app": "Orca", "command": "orca file open", "arbitrated": True,
                }}},
                "win32": win32_partition,
            },
        }, ensure_ascii=False), encoding="utf-8")
        cfg = self._init(platform="darwin", force_rescan=True)
        self.assertNotEqual(cfg["platforms"]["darwin"]["associations"]["markdown"]["command"], "orca file open")
        self.assertEqual(cfg["platforms"]["win32"], win32_partition)  # 全量重扫只重建当前平台分区


class RolePreferenceBootstrapTests(TestCase):
    def test_local_detection_preserves_new_nonselected_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "roles.yaml"
            original = (
                "roles:\n"
                "  Builder:\n"
                "    duty: build\n"
                "    preferences: [local-builder]\n"
                "command_profiles:\n"
                "  local-builder: {agent: codex, model: openai/gpt-test}\n"
            )
            config.write_text(original, encoding="utf-8")

            fake_codex = Path(tmp) / "codex"
            fake_codex.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = exec ] && [ \"$2\" = --help ]; then\n"
                "  printf '%s\\n' --model\n"
                "else\n"
                "  exit 99\n"
                "fi\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)

            with mock.patch.object(role_preferences.shutil, "which", return_value=str(fake_codex)):
                report = detect_role_preferences(config)

            self.assertNotIn("selected_profile", report[0])
            self.assertEqual(report[0]["preferences"][0]["status"], "local_ready")
            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_missing_role_config_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(detect_role_preferences(Path(tmp) / "missing.yaml"), [])

