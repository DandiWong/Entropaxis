"""单元测试：胶囊外部依赖（dependencies）契约、断链扫描、漂移检测与级联移动。"""

import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tools import apply_moves, init_capsule, lint_workspace as lw, update_capsule as uc, validate_schema as vs


class CapsuleDependenciesTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_deps_")
        self.root = Path(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_schema_allows_dependencies(self):
        """测试 capsule.schema.json 正常支持 dependencies 字符串和对象形式。"""
        schema = vs.load_schema("capsule")
        data = {
            "id": "Tech-1",
            "topic": "测试依赖",
            "mode": "light",
            "lifecycle": "draft",
            "dependencies": [
                "01_资料/合同.docx",
                {
                    "path": "02_原型Demo/demo.html",
                    "role": "living_asset",
                    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "captured_at": "2026-10-19T10:00:00+08:00",
                },
            ],
            "worklogs": [],
        }
        errs = vs.validate(data, schema)
        self.assertEqual(errs, [], f"dependencies 未通过 Schema 校验: {errs}")

    def test_lint_detects_broken_dependency(self):
        """测试 lint_workspace 能机械检出声明了但不存在的外部依赖。"""
        cap = init_capsule.create_capsule(self.root / "proj", "测试断链", "Tech-2", "light")
        manifest = cap / "capsule.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + "dependencies:\n  - ../../不存在的文件.html\n",
            encoding="utf-8",
        )
        issues = lw.check_routing_integrity(self.root)
        broken = [i for i in issues if "胶囊依赖断链" in i]
        self.assertTrue(len(broken) >= 1, f"未检出胶囊依赖断链: {issues}")
        self.assertIn("不存在的文件.html", broken[0])

    def test_lint_detects_sha_drift(self):
        """测试外部资产内容被改动时，能检出 SHA256 漂移。"""
        ext_file = self.root / "proj" / "demo.html"
        ext_file.parent.mkdir(parents=True, exist_ok=True)
        ext_file.write_text("v1 内容", encoding="utf-8")
        orig_sha = hashlib.sha256(ext_file.read_bytes()).hexdigest()

        cap = init_capsule.create_capsule(self.root / "proj", "测试漂移", "Tech-3", "light")
        manifest = cap / "capsule.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8")
            + f"dependencies:\n  - path: ../demo.html\n    sha256: {orig_sha}\n",
            encoding="utf-8",
        )

        # 初始应无漂移
        issues = [i for i in lw.check_routing_integrity(self.root) if "胶囊依赖漂移" in i]
        self.assertEqual(issues, [])

        # 外部篡改内容
        ext_file.write_text("v2 被篡改内容", encoding="utf-8")
        issues_after = [i for i in lw.check_routing_integrity(self.root) if "胶囊依赖漂移" in i]
        self.assertEqual(len(issues_after), 1)
        self.assertIn("demo.html", issues_after[0])

    def test_apply_moves_cascades_dependencies(self):
        """测试 apply_moves 移动外部资产时，能自动级联修正胶囊中的相对路径。"""
        src_file = self.root / "proj" / "old_demo.html"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("原型代码", encoding="utf-8")

        cap = init_capsule.create_capsule(self.root / "proj", "测试级联", "Tech-4", "light")
        manifest = cap / "capsule.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + "dependencies:\n  - ../old_demo.html\n",
            encoding="utf-8",
        )

        moves_tsv = self.root / "moves.tsv"
        moves_tsv.write_text("proj/old_demo.html\tproj/new_dir/new_demo.html\n", encoding="utf-8")

        ret = apply_moves.run(moves_tsv, root=self.root, apply=True)
        self.assertEqual(ret, apply_moves.EXIT_OK)

        # 检查胶囊中的引用是否已被自动级联修正
        updated_content = manifest.read_text(encoding="utf-8")
        self.assertIn("new_demo.html", updated_content)
        self.assertNotIn("old_demo.html", updated_content)


if __name__ == "__main__":
    unittest.main()
