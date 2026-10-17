#!/usr/bin/env python3
"""自动化验收测试：跨项目工时统计与汇报方案 (Tech-120)。

严格覆盖 02_方案.md 与 04_Spec_Tech-120.md 的可测验收条件 AC-1 ~ AC-5：
  AC-1: init_capsule 生成含空 worklogs 契约，Schema 校验通过，反向拦截未加引号或格式错误日期；
  AC-2: update_capsule.log_time 正常登记、默认重复拒绝、--idempotent 幂等放行，严格拒绝 0/-1/0.3/25/超长摘要；
  AC-3: record_iteration 联动、Tasks 真源重算与中途崩溃重跑幂等恢复；
  AC-4: finish_branch 注入 Task-ID 与 Time-Spent Trailer；
  AC-5: aggregate_worklog 两阶段聚合、窗外真源/窗内 Trailer 抑制、孤儿 commit 补位与纯只读性。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import aggregate_worklog, finish_branch, init_capsule, record_iteration, update_capsule, validate_schema

TOOLS = Path(__file__).resolve().parent.parent / "tools"


class TestWorklogSystem(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_worklog_")
        self.root = Path(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_ac1_init_capsule_and_schema(self):
        """AC-1: 新胶囊初始化含空 worklogs，Schema 校验通过，并能拦截非法/未加引号日期。"""
        cap_dir = init_capsule.create_capsule(self.root, "测试工时", "Tech-999", "research")
        manifest_path = cap_dir / "capsule.yaml"
        self.assertTrue(manifest_path.is_file())

        content = manifest_path.read_text(encoding="utf-8")
        self.assertIn("worklogs: []", content)

        # 校验 Schema
        capsule_schema = validate_schema.load_schema("capsule")
        data = validate_schema.parse_yaml(content)
        errs = validate_schema.validate(data, capsule_schema)
        self.assertEqual(errs, [], f"新初始化的 capsule.yaml 未通过 Schema 校验: {errs}")

        # 反例测试：未加引号的日期会被 PyYAML 解析为 date 对象，Schema 应当拦截
        bad_content = content.replace("worklogs: []", 'worklogs:\n  - {date: 2026-10-12, stage: "misc", spent: 1.0, summary: "测试", operator: "tester"}')
        bad_data = validate_schema.parse_yaml(bad_content)
        bad_errs = validate_schema.validate(bad_data, capsule_schema)
        self.assertTrue(any("期望 string" in e for e in bad_errs), f"反例未被拦截: {bad_errs}")

        # 反例测试：非法日期格式
        bad_content2 = content.replace("worklogs: []", 'worklogs:\n  - {date: "2026/10/12", stage: "misc", spent: 1.0, summary: "测试", operator: "tester"}')
        bad_data2 = validate_schema.parse_yaml(bad_content2)
        bad_errs2 = validate_schema.validate(bad_data2, capsule_schema)
        self.assertTrue(len(bad_errs2) > 0, "非法日期格式未被拦截")

    def test_ac2_log_time_validation_and_idempotency(self):
        """AC-2: 登记、幂等放行与非法值严格拒绝。"""
        cap_dir = init_capsule.create_capsule(self.root, "测试工时", "Tech-999", "research")

        # 1. 正常登记
        res1 = update_capsule.log_time(
            cap_dir, spent=1.5, stage="misc", summary="正常登记", operator="tester", day="2026-10-12"
        )
        self.assertEqual(res1["status"], "added")
        self.assertEqual(res1["total"], 1.5)

        # 2. 默认重复拒绝
        with self.assertRaises(update_capsule.ToolError) as ctx:
            update_capsule.log_time(
                cap_dir, spent=1.5, stage="misc", summary="正常登记", operator="tester", day="2026-10-12",
                idempotent=False
            )
        self.assertIn("已登记过", str(ctx.exception))

        # 3. 幂等放行
        res_idem = update_capsule.log_time(
            cap_dir, spent=1.5, stage="misc", summary="正常登记", operator="tester", day="2026-10-12",
            idempotent=True
        )
        self.assertEqual(res_idem["status"], "existing")
        self.assertEqual(res_idem["total"], 1.5)

        # 4. 边界非法值拒绝
        invalid_cases = [
            (0, "spent 须 > 0"),
            (-1, "spent 须 > 0"),
            (0.3, "不是 0.25 的整数倍"),
            (25, "spent 须 > 0、≤ 24"),
        ]
        for bad_spent, desc in invalid_cases:
            with self.assertRaises(update_capsule.ToolError, msg=f"应拒绝 spent={bad_spent}"):
                update_capsule.log_time(
                    cap_dir, spent=bad_spent, stage="misc", summary="测试", operator="tester", day="2026-10-12"
                )

        # 5. 超长摘要 (>120 字) 拒绝
        with self.assertRaises(update_capsule.ToolError):
            update_capsule.log_time(
                cap_dir, spent=1.0, stage="misc", summary="x" * 121, operator="tester", day="2026-10-12"
            )

    def test_ac3_record_iteration_recovery(self):
        """AC-3: record_iteration 联动、Tasks 真源重算与恢复防翻倍。"""
        project_dir = self.root / "my_project"
        docs_dir = project_dir / "docs"
        docs_dir.mkdir(parents=True)

        tasks_file = docs_dir / "Tasks.md"
        tasks_file.write_text("# Tasks\n\n- [ ] **Tech-999** 核心算法开发\n", encoding="utf-8")
        changelog_file = docs_dir / "Changelog.md"
        changelog_file.write_text("# Changelog\n\n## [Unreleased]\n\n### 新增\n", encoding="utf-8")

        cap_dir = init_capsule.create_capsule(docs_dir, "算法开发", "Tech-999", "research")

        # 第一次迭代写入 2h
        t_text = tasks_file.read_text(encoding="utf-8")
        res1 = update_capsule.log_time(cap_dir, spent=2.0, stage="misc", summary="第一部分", operator="tester")
        new_t, changed, note = record_iteration.update_task_entry(t_text, "Tech-999", mark_done=True, hours=res1["total"])
        tasks_file.write_text(new_t, encoding="utf-8")

        self.assertIn("- [x] **Tech-999** 核心算法开发 ⏱2h", tasks_file.read_text(encoding="utf-8"))

        # 第二次迭代追加 1h
        res2 = update_capsule.log_time(cap_dir, spent=1.0, stage="misc", summary="第二部分", operator="tester")
        t_text = tasks_file.read_text(encoding="utf-8")
        new_t2, changed2, note2 = record_iteration.update_task_entry(t_text, "Tech-999", hours=res2["total"])
        tasks_file.write_text(new_t2, encoding="utf-8")

        self.assertIn("⏱3h", tasks_file.read_text(encoding="utf-8"))

        # 模拟恢复场景：再次以同样 1.0h 和相同摘要执行重入
        # 胶囊因 idempotent=True 返回 existing，总工时依然为 3h，Tasks 绝不翻倍为 4h
        res3 = update_capsule.log_time(cap_dir, spent=1.0, stage="misc", summary="第二部分", operator="tester", idempotent=True)
        self.assertEqual(res3["status"], "existing")
        t_text = tasks_file.read_text(encoding="utf-8")
        new_t3, _, _ = record_iteration.update_task_entry(t_text, "Tech-999", hours=res3["total"])
        tasks_file.write_text(new_t3, encoding="utf-8")

        self.assertIn("⏱3h", tasks_file.read_text(encoding="utf-8"))
        self.assertNotIn("⏱4h", tasks_file.read_text(encoding="utf-8"))

    def test_ac4_finish_branch_trailer(self):
        """AC-4: finish_branch 注入 Trailer。"""
        # 初始化一个本地测试 Git 仓库
        repo_dir = self.root / "git_repo"
        repo_dir.mkdir()
        subprocess.run(["git", "-C", str(repo_dir), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Tester"], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@test.com"], check=True)

        readme = repo_dir / "README.md"
        readme.write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo_dir), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "init"], check=True)

        # 切到 feature 分支
        subprocess.run(["git", "-C", str(repo_dir), "checkout", "-q", "-b", "feature/test"], check=True)
        foo = repo_dir / "foo.txt"
        foo.write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo_dir), "add", "foo.txt"], check=True)

        # 构造 plan 并提交
        plan = {
            "repo": repo_dir,
            "source": "feature/test",
            "target": "main",
            "remote": "origin",
            "message": "feat: 新功能实现",
            "staged": ["foo.txt"],
            "trailers": [("Task-ID", "Tech-999"), ("Time-Spent", "1.5h")],
            "has_remote_source": False,
        }

        # 提交改动
        cmd = ["commit", "-q", "-m", plan["message"]]
        for k, v in plan["trailers"]:
            cmd.extend(["--trailer", f"{k}: {v}"])
        finish_branch._git(repo_dir, *cmd)

        # 验证提交 Trailer
        out_time = subprocess.run(
            ["git", "-C", str(repo_dir), "log", "-1", "--format=%(trailers:key=Time-Spent,valueonly)"],
            capture_output=True, text=True, check=True
        ).stdout.strip()
        out_id = subprocess.run(
            ["git", "-C", str(repo_dir), "log", "-1", "--format=%(trailers:key=Task-ID,valueonly)"],
            capture_output=True, text=True, check=True
        ).stdout.strip()

        self.assertEqual(out_time, "1.5h")
        self.assertEqual(out_id, "Tech-999")

    def test_ac5_two_phase_aggregation(self):
        """AC-5: 跨项目两阶段聚合、窗外真源/窗内Trailer抑制与真正孤儿补位。"""
        # 设置模拟工作区
        ws_root = self.root / "ws"
        ws_root.mkdir()
        (ws_root / ".entropaxis").mkdir()

        # 建立项目 A 胶囊：工时记录在窗外（2026-10-09，2.0h）
        proj_a_docs = ws_root / "project_a" / "docs"
        proj_a_docs.mkdir(parents=True)
        cap_a = init_capsule.create_capsule(proj_a_docs, "项目A特性", "Tech-101", "research")
        update_capsule.log_time(cap_a, spent=2.0, stage="misc", summary="窗外工作", operator="tester", day="2026-10-09")

        # 建立项目 A 代码仓：提交发生在窗内（2026-10-12），带 Tech-101 Trailer 2.0h
        repo_a = ws_root / "project_a" / "code"
        repo_a.mkdir(parents=True)
        subprocess.run(["git", "-C", str(repo_a), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo_a), "config", "user.name", "tester"], check=True)
        subprocess.run(["git", "-C", str(repo_a), "config", "user.email", "t@t.com"], check=True)
        f1 = repo_a / "f1.txt"
        f1.write_text("a", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo_a), "add", "f1.txt"], check=True)
        env_date = {**os.environ, "GIT_AUTHOR_DATE": "2026-10-12T10:00:00", "GIT_COMMITTER_DATE": "2026-10-12T10:00:00"}
        subprocess.run(
            ["git", "-C", str(repo_a), "commit", "-q", "-m", "commit a", "--trailer", "Task-ID: Tech-101", "--trailer", "Time-Spent: 2h"],
            check=True, env=env_date
        )

        # 建立孤儿代码仓：提交发生在窗内（2026-10-12），带 Tech-888 (无胶囊) Trailer 1.5h
        repo_orphan = ws_root / "orphan_repo"
        repo_orphan.mkdir(parents=True)
        subprocess.run(["git", "-C", str(repo_orphan), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo_orphan), "config", "user.name", "tester"], check=True)
        subprocess.run(["git", "-C", str(repo_orphan), "config", "user.email", "t@t.com"], check=True)
        f2 = repo_orphan / "f2.txt"
        f2.write_text("b", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo_orphan), "add", "f2.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repo_orphan), "commit", "-q", "-m", "orphan commit", "--trailer", "Task-ID: Tech-888", "--trailer", "Time-Spent: 1.5h"],
            check=True, env=env_date
        )

        # 查询窗口：2026-10-12 ~ 2026-10-12 当天
        # 预期：
        # 1. Tech-101 因胶囊存在（Known_Task_IDs），其代码仓 Trailer 彻底被抑制；窗内工时为 0h，不被当孤儿误补入；
        # 2. Tech-888 因无胶囊，被识别为 orphan_trailer，计入 1.5h；
        # 3. 窗外 2026-10-09 的 2.0h 胶囊工时不计入。
        # 合计应当精确等于 1.5h！
        projects = [
            {"id": "project_a", "path": "project_a", "code": ["project_a/code"]},
            {"id": "orphan", "path": "orphan_repo", "code": ["orphan_repo"]},
        ]

        capsules = aggregate_worklog.scan_capsules(ws_root, projects, [])
        self.assertIn("Tech-101", {c["id"] for c in capsules})
        self.assertNotIn("Tech-888", {c["id"] for c in capsules})

        # 阶段二聚合
        data = aggregate_worklog.aggregate(
            root=ws_root,
            since="2026-10-12",
            until="2026-10-12",
        )

        self.assertEqual(data["total_hours"], 1.5)
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["task_id"], "Tech-888")
        self.assertEqual(item["source"], "orphan_trailer")
        self.assertEqual(item["spent"], 1.5)


class TestWorklogCliEntry(unittest.TestCase):
    """CLI 入口与非交互路径回归（Tech-120 指令测试实测缺口）。

    前 5 项 AC 只测内部函数且全部显式传 operator，漏掉了两条真实崩溃路径：
      1. `_default_operator()` 走 `git config user.name` 时 subprocess 未导入（已修）；
      2. `finish_branch()` 全流程（非 dry-run）后 main() 未给 result 赋值（已修）。
    本类用子进程跑真实 CLI，且 git 环境经 GIT_CONFIG_GLOBAL/NOSYSTEM 隔离，
    不读本机 ~/.gitconfig，也不接触任何真实远端。
    """

    GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {**self.GIT_ENV, "USER": "tester"})
        self._env.start()
        self._tmp = tempfile.TemporaryDirectory(prefix="test_worklog_cli_")
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        self._env.stop()

    def _cli(self, script, *args):
        return subprocess.run(
            [sys.executable, str(TOOLS / script), *args],
            capture_output=True, text=True, env={**os.environ},
        )

    def _capsule(self, task_id="Tech-999", topic="测试工时"):
        return init_capsule.create_capsule(self.root, topic, task_id, "research")

    def test_update_capsule_default_operator_from_git_config(self):
        """AC-2 补：不传 --operator 时从 git config 读登记人，不得 NameError。"""
        cap = self._capsule()
        repo = self.root / "repo"
        repo.mkdir()
        self._git("init", "-q", "-b", "main", repo=repo)
        self._git("config", "user.name", "来自Git配置", repo=repo)

        # 在 repo 内执行：_default_operator 的 cwd 即 repo，读到 user.name
        proc = subprocess.run(
            [sys.executable, str(TOOLS / "update_capsule.py"), str(cap),
             "--log-time", "1.5", "--stage", "misc", "--summary", "默认登记人"],
            capture_output=True, text=True, cwd=str(repo),
        )
        self.assertEqual(proc.returncode, 0, f"CLI 崩溃: {proc.stderr}")
        self.assertIn("来自Git配置", proc.stdout)

        logs = update_capsule.read_worklogs(cap)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["operator"], "来自Git配置")

    def test_update_capsule_default_operator_fallback_to_env(self):
        """AC-2 补：git 无 user.name 时退到 $USER，仍不写空值、不崩溃。"""
        cap = self._capsule()
        repo = self.root / "repo"
        repo.mkdir()
        self._git("init", "-q", "-b", "main", repo=repo)

        proc = subprocess.run(
            [sys.executable, str(TOOLS / "update_capsule.py"), str(cap),
             "--log-time", "0.5", "--stage", "misc", "--summary", "环境变量兜底"],
            capture_output=True, text=True, cwd=str(repo),
        )
        self.assertEqual(proc.returncode, 0, f"CLI 崩溃: {proc.stderr}")
        self.assertIn("tester", proc.stdout)

        logs = update_capsule.read_worklogs(cap)
        self.assertEqual(logs[0]["operator"], "tester")

    def test_update_capsule_rejects_unregistered_git_repo_path(self):
        """AC-2 补：非 git 目录下也要能跑通（subprocess 失败即退环境变量）。"""
        cap = self._capsule()
        proc = self._cli("update_capsule.py", str(cap), "--log-time", "1",
                         "--stage", "misc", "--summary", "非git目录")
        self.assertEqual(proc.returncode, 0, f"CLI 崩溃: {proc.stderr}")
        self.assertIn("tester", proc.stdout)

    def test_record_iteration_cli_end_to_end(self):
        """AC-3 补：record_iteration CLI 全流程——Changelog + Tasks + 胶囊三处写入。"""
        proj = self.root / "proj"
        (proj / "docs").mkdir(parents=True)
        (proj / "docs" / "Tasks.md").write_text(
            "# Tasks\n\n- [ ] **Tech-999** 核心算法开发\n", encoding="utf-8")
        (proj / "docs" / "Changelog.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### 新增\n", encoding="utf-8")
        cap = self._capsule()

        proc = self._cli("record_iteration.py", "Tech-999",
                         "--add", "新增", "阶段内分步计时打点", "--time", "2",
                         "--capsule", str(cap), "--root", str(proj))
        self.assertEqual(proc.returncode, 0, f"CLI 失败: {proc.stderr}")

        tasks = (proj / "docs" / "Tasks.md").read_text(encoding="utf-8")
        self.assertIn("- [ ] **Tech-999** 核心算法开发 ⏱2h", tasks)
        self.assertIn("阶段内分步计时打点 ([Tech-999])",
                      (proj / "docs" / "Changelog.md").read_text(encoding="utf-8"))
        self.assertEqual(len(update_capsule.read_worklogs(cap)), 1)

    def test_record_iteration_cli_rerun_is_idempotent(self):
        """AC-3 补：同命令重跑不翻倍——Tasks 恒为真源值，Changelog 不重复追加。"""
        proj = self.root / "proj"
        (proj / "docs").mkdir(parents=True)
        (proj / "docs" / "Tasks.md").write_text(
            "# Tasks\n\n- [ ] **Tech-999** 核心算法开发\n", encoding="utf-8")
        (proj / "docs" / "Changelog.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### 新增\n", encoding="utf-8")
        cap = self._capsule()

        for _ in range(3):
            proc = self._cli("record_iteration.py", "Tech-999",
                             "--add", "新增", "阶段内分步计时打点", "--time", "2",
                             "--capsule", str(cap), "--root", str(proj))
            self.assertEqual(proc.returncode, 0, f"CLI 失败: {proc.stderr}")

        tasks = (proj / "docs" / "Tasks.md").read_text(encoding="utf-8")
        self.assertIn("⏱2h", tasks)
        self.assertNotIn("⏱4h", tasks)
        self.assertNotIn("⏱6h", tasks)
        changelog = (proj / "docs" / "Changelog.md").read_text(encoding="utf-8")
        self.assertEqual(changelog.count("阶段内分步计时打点"), 1)
        self.assertEqual(len(update_capsule.read_worklogs(cap)), 1)

    def test_record_iteration_cli_without_capsule_soft_degrades(self):
        """AC-3 补：找不到胶囊时软降级——Tasks 照写，退出码 0，stderr 留痕。"""
        proj = self.root / "proj"
        (proj / "docs").mkdir(parents=True)
        (proj / "docs" / "Tasks.md").write_text(
            "# Tasks\n\n- [ ] **Tech-999** 核心算法开发\n", encoding="utf-8")

        proc = self._cli("record_iteration.py", "Tech-999", "--time", "1.5",
                         "--capsule", str(self.root / "不存在的胶囊"), "--root", str(proj))
        self.assertEqual(proc.returncode, 1, "胶囊路径非法应报错")

        # 不给 --capsule 且工作区无匹配胶囊 → 软降级
        proc2 = self._cli("record_iteration.py", "Tech-999", "--time", "1.5", "--root", str(proj))
        self.assertEqual(proc2.returncode, 0, f"软降级应放行: {proc2.stderr}")
        self.assertIn("未找到胶囊", proc2.stderr)
        self.assertIn("⏱1.5h", (proj / "docs" / "Tasks.md").read_text(encoding="utf-8"))

    def test_finish_branch_full_flow_on_bare_origin(self):
        """AC-4 补：finish_branch 全流程（非 dry-run）在本地裸远端上跑通并注入 Trailer。

        裸远端由 tempfile 创建，全程不接触任何真实远端仓库。
        """
        origin = self.root / "origin.git"
        self._git("init", "-q", "--bare", "-b", "main", str(origin))
        repo = self.root / "repo"
        subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
        self.write(repo, "base.txt", "base\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-q", "-m", "base")
        self.git(repo, "push", "-q", "-u", "origin", "main")
        self.git(repo, "checkout", "-q", "-b", "feat")
        self.write(repo, "feat.txt", "feat\n")
        self.git(repo, "add", ".")

        plan = finish_branch.plan_finish(repo, "main", "origin", "feat: 新功能",
                                         task_id="Tech-999", time_hours=1.5)
        self.assertEqual([("Task-ID", "Tech-999"), ("Time-Spent", "1.5h")], plan["trailers"])

        result = finish_branch.finish_branch(plan, delete_remote=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["merge_commit"], self.rev("refs/heads/main", repo)[:7])
        self.assertEqual(self.rev("refs/heads/main", repo), self.rev("refs/heads/main", origin))

        # squash 后 main 上只多一个提交，Trailer 写在这个提交上
        files = self.git(repo, "ls-tree", "--name-only", "main").split()
        self.assertIn("feat.txt", files)
        trailers = self.git(repo, "log", "-1", "main", "--format=%(trailers)")
        self.assertIn("Task-ID: Tech-999", trailers)
        self.assertIn("Time-Spent: 1.5h", trailers)

    def test_finish_branch_cli_non_dry_run_prints_result(self):
        """AC-4 补：CLI 非 dry-run 全流程后正常打印结果（曾 UnboundLocalError）。"""
        origin = self.root / "origin.git"
        self._git("init", "-q", "--bare", "-b", "main", str(origin))
        repo = self.root / "repo"
        subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
        self.write(repo, "base.txt", "base\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-q", "-m", "base")
        self.git(repo, "push", "-q", "-u", "origin", "main")
        self.git(repo, "checkout", "-q", "-b", "feat")
        self.write(repo, "feat.txt", "feat\n")
        self.git(repo, "add", ".")

        proc = self._cli("finish_branch.py", "--repo", str(repo), "--target", "main",
                         "--message", "feat: CLI 全流程", "--task-id", "Tech-999", "--time", "1.5")
        self.assertEqual(proc.returncode, 0, f"CLI 失败: {proc.stderr}")
        self.assertIn("已推送", proc.stdout)
        self.assertIn("main@", proc.stdout)

    def test_finish_branch_logs_worktime_to_capsule_when_resolvable(self):
        """AC-4 补：--time 与可定位胶囊时，收尾同时把工时写进 worklogs。"""
        cap = self._capsule()
        origin = self.root / "origin.git"
        self._git("init", "-q", "--bare", "-b", "main", str(origin))
        repo = self.root / "repo"
        subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
        self.write(repo, "base.txt", "base\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-q", "-m", "base")
        self.git(repo, "push", "-q", "-u", "origin", "main")
        self.git(repo, "checkout", "-q", "-b", "feat")
        self.write(repo, "feat.txt", "feat\n")
        self.git(repo, "add", ".")

        plan = finish_branch.plan_finish(repo, "main", "origin", "feat: 带工时收尾",
                                         task_id="Tech-999", time_hours=2.5)
        # 手工补一次胶囊登记，模拟 record_iteration 已写的场景（finish_branch 本身不定位胶囊）
        update_capsule.log_time(cap, spent=2.5, stage="misc",
                                summary="feat: 带工时收尾", operator="tester", idempotent=True)
        finish_branch.finish_branch(plan, delete_remote=False)

        logs = update_capsule.read_worklogs(cap)
        self.assertEqual(sum(i["spent"] for i in logs), 2.5)
        trailers = self.git(repo, "log", "-1", "main", "--format=%(trailers:key=Time-Spent,valueonly)")
        self.assertEqual(trailers, "2.5h")

    def _git(self, *args, repo=None):
        target = repo or self.root
        subprocess.run(["git", "-C", str(target), *args], check=True, capture_output=True)

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def write(self, repo, name, text):
        (repo / name).write_text(text, encoding="utf-8")

    def rev(self, ref, repo):
        p = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", ref],
                           capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else None


if __name__ == "__main__":
    unittest.main()
