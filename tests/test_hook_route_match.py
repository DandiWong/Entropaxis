import json
import unittest
from pathlib import Path

from tools.hook_route_match import (
    PRECEDENCE_NOTICE,
    ROUTE_MAP_PATH,
    build_additional_context,
    load_routes,
    match_prompt,
)

SYSTEM_ROOT = Path(__file__).resolve().parent.parent


class HookRouteMatchTests(unittest.TestCase):
    def test_route_map_file_exists_and_parses(self) -> None:
        self.assertTrue(ROUTE_MAP_PATH.exists(), "route_map.json must exist")
        routes = load_routes()
        self.assertTrue(routes, "route_map.json must declare at least one route")
        for route in routes:
            self.assertIn("mechanism", route)
            self.assertIn("files", route)
            self.assertIn("keywords", route)

    def test_route_map_targets_exist(self) -> None:
        workspace_root = SYSTEM_ROOT.parent
        for route in load_routes():
            for f in route["files"]:
                self.assertTrue(
                    (workspace_root / f).exists(),
                    f"route target {f} referenced by mechanism {route['mechanism']} does not exist",
                )

    def test_substring_match_hits_audit_mechanism(self) -> None:
        routes = load_routes()
        hits = match_prompt("帮我对最新方案做一次方案审计", routes)
        mechanisms = {h["mechanism"] for h in hits}
        self.assertIn("审计", mechanisms)

    def test_no_match_returns_empty(self) -> None:
        routes = load_routes()
        hits = match_prompt("今天天气怎么样", routes)
        self.assertEqual(hits, [])

    def test_exact_match_type_requires_full_string(self) -> None:
        routes = [{"mechanism": "tip", "files": ["x.md"], "anchor": "#", "match_type": "exact", "keywords": ["tip"]}]
        self.assertEqual(match_prompt("tip", routes), routes)
        self.assertEqual(match_prompt("tips for you", routes), [])

    def test_build_additional_context_dedupes_files(self) -> None:
        hits = [
            {"mechanism": "同步任务", "files": [".system/rules/指令解析.md"], "anchor": "## 同步任务"},
            {"mechanism": "同步", "files": [".system/rules/指令解析.md"], "anchor": "## 同步任务"},
        ]
        context = build_additional_context(hits)
        self.assertEqual(context.count(".system/rules/指令解析.md"), 1)

    def test_build_additional_context_empty_for_no_hits(self) -> None:
        self.assertEqual(build_additional_context([]), "")

    def test_build_additional_context_asserts_precedence_over_persona_instructions(self) -> None:
        hits = [{"mechanism": "审计", "files": [".system/rules/指令解析.md"], "anchor": "## 审计"}]
        self.assertIn(PRECEDENCE_NOTICE, build_additional_context(hits))

    def test_load_routes_missing_file_degrades_gracefully(self) -> None:
        self.assertEqual(load_routes(Path("/nonexistent/route_map.json")), [])


if __name__ == "__main__":
    unittest.main()
