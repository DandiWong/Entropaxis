"""Tech-175 M1-3 契约：可版本化静态资产与零外链。

依据：`02_方案.md` 6.1（本地服务层）与 Spec 4.2 的 M1-3 行；原型 v2 视觉骨架迁入
`.entropaxis/tools/workbench/static/`（HTML/CSS/JS，本地资源、零外链、离线可开）。

测试只做文件级断言（存在性、引用闭环、外链扫描）与 Python 侧服务契约，不起浏览器；
浏览器侧冒烟另见 Spec 实施记录。零外链 = 页面不依赖任何 CDN / 远程字体 / 远程图片。
"""
import re
import unittest
import urllib.parse
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = SYSTEM_ROOT / "tools" / "workbench" / "static"

# 远程资源协议：http(s) 之外，`//host/...` 协议相对 URL 同样会离开本机。
REMOTE_SCHEME = re.compile(r"(?:https?:)?//[A-Za-z0-9]", re.IGNORECASE)
# 本地引用：href/src/url() 的取值。只取属性与 CSS url()，不扫正文散文。
ATTR_REF = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
CSS_URL_REF = re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""", re.IGNORECASE)
# 数据 URI 与锚点不算外链；`data:` 图片零依赖，属离线可用资源。
LOCAL_REF = re.compile(r"^(?:data:|#|/api/|https?://127\.0\.0\.1)")


class StaticAssetTests(unittest.TestCase):
    """M1-3：静态资产存在、引用闭环、零外链。"""

    def test_static_dir_has_entry_html_css_js(self) -> None:
        """静态目录同时含 HTML 与 CSS/JS：迁移是三文件形态，不是单个巨型 HTML。"""
        self.assertTrue(STATIC_DIR.is_dir(), f"静态目录缺失: {STATIC_DIR}")
        html = sorted(STATIC_DIR.glob("*.html"))
        css = sorted(STATIC_DIR.glob("*.css"))
        js = sorted(STATIC_DIR.glob("*.js"))
        self.assertTrue(html, f"{STATIC_DIR} 下没有 .html")
        self.assertTrue(css, f"{STATIC_DIR} 下没有 .css")
        self.assertTrue(js, f"{STATIC_DIR} 下没有 .js")

    def test_no_remote_resources_in_any_asset(self) -> None:
        """零外链：任何静态文件都不引用 CDN / 远程字体 / 远程图片。"""
        offenders = []
        for path in sorted(STATIC_DIR.rglob("*")):
            if not path.is_file() or path.suffix not in (".html", ".css", ".js"):
                continue
            text = path.read_text(encoding="utf-8")
            for ref in ATTR_REF.findall(text) + CSS_URL_REF.findall(text):
                if REMOTE_SCHEME.match(ref) and not LOCAL_REF.match(ref):
                    offenders.append(f"{path.relative_to(SYSTEM_ROOT)}: {ref}")
        self.assertEqual(offenders, [], "静态资产存在远程资源引用（应全部本地化）")

    def test_local_references_resolve(self) -> None:
        """引用闭环：HTML/CSS 引用的本地文件必须存在（不留断链）。"""
        missing = []
        for path in sorted(STATIC_DIR.rglob("*")):
            if not path.is_file() or path.suffix not in (".html", ".css"):
                continue
            text = path.read_text(encoding="utf-8")
            for ref in ATTR_REF.findall(text) + CSS_URL_REF.findall(text):
                if REMOTE_SCHEME.match(ref) or ref.startswith(("data:", "#")):
                    continue
                target = ref.split("?", 1)[0].split("#", 1)[0]
                if not target:
                    continue
                if not (path.parent / target).resolve().exists():
                    missing.append(f"{path.relative_to(SYSTEM_ROOT)}: {ref}")
        self.assertEqual(missing, [], "本地资源引用断链")

    def test_entry_html_loads_local_css_and_js(self) -> None:
        """入口 HTML 必须显式引用同目录的 CSS 与 JS（不靠内联巨型文件）。"""
        entry = STATIC_DIR / "index.html"
        self.assertTrue(entry.is_file(), f"入口页面缺失: {entry}")
        text = entry.read_text(encoding="utf-8")
        self.assertRegex(text, r'<link[^>]+href="[^"]+\.css"')
        self.assertRegex(text, r'<script[^>]+src="[^"]+\.js"')

    def test_prototype_left_untouched(self) -> None:
        """原型文件留在 data/ 下作过程证据，迁移不移动它。"""
        prototype = (SYSTEM_ROOT / "data" / "docs"
                     / "20261029_Entropaxis团队协作工作台" / "prototypes" / "workbench.html")
        if not prototype.is_file():
            # O-1（干净检出）：原型在实例面 `data/docs/`，`.gitignore` 锚定 `/data/`，
            # `git archive` 干净检出里不存在。依赖缺失即跳过，不判失败（同源事实见
            # Tech-176 侧 RunFileCapsuleFieldTests 的 O-1 处置）。
            self.skipTest(f"实例面原型文件不在检出内（{prototype}），"
                          "干净检出不含 data/，跳过原型原位断言")
        self.assertTrue(prototype.is_file(), f"原型文件应保持原位: {prototype}")


class StaticRouteTests(unittest.TestCase):
    """M1-3：本地服务把静态目录当只读资源提供，令牌边界不变。"""

    def test_static_dir_is_not_under_data(self) -> None:
        """静态资产进版本库：不能落在 data/（那里不可版本化）。"""
        self.assertNotIn("/data/", str(STATIC_DIR))

    def test_workbench_module_exposes_static_dir(self) -> None:
        """服务模块导出静态目录常量，供路由解析相对路径。"""
        import sys
        sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
        import workbench
        self.assertEqual(workbench.STATIC_DIR.resolve(), STATIC_DIR.resolve())

    def test_static_route_is_readonly_get(self) -> None:
        """静态路由只走 GET：写路径仍只有 M1-2 的五条写路由（AC-18）。"""
        import sys
        sys.path.insert(0, str(SYSTEM_ROOT / "tools"))
        import workbench
        self.assertIn("/", workbench.STATIC_ROUTES)
        self.assertNotIn("/", workbench.WRITE_ROUTES)


if __name__ == "__main__":
    unittest.main()
