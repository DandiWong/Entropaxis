import tempfile
import unittest
from pathlib import Path

from tools.hook_route_match import build_additional_context, load_routes, match_prompt
from tools.route_context import resolve_route_reads
from tools.lint_workspace import check_route_map_integrity
from tools.validate_schema import load_schema, validate


class HookRouteMatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.system/rules').mkdir(parents=True)

    def rule(self, name, text):
        file = '.system/rules/' + name + '.md'
        (self.root / file).write_text(text, encoding='utf-8')
        return file

    def test_explicit_files_keep_own_sections_and_scope(self):
        first = self.rule('first', '# First\nApplies locally.\n## Select\nNeeded.\n## Other\nUnrelated.\n')
        second = self.rule('second', '# Second\nDifferent scope.\n## Dependency\nRequired.\n## Else\nUnused.\n')
        route = {'mechanism': 'test', 'reads': [
            {'file': first, 'anchor': '## Select'},
            {'file': second, 'anchor': '## Dependency'},
        ]}
        plan = resolve_route_reads(route, self.root)
        self.assertEqual([r['text'] for r in plan], [
            '# First\nApplies locally.\n## Select\nNeeded.\n',
            '# Second\nDifferent scope.\n## Dependency\nRequired.\n',
        ])
        context = build_additional_context([route], self.root)
        self.assertIn(first + ':1-4', context)
        self.assertIn(second + ':1-4', context)
        self.assertNotIn(':1-6', context)

    def test_nested_scope_and_fences_do_not_hide_required_text(self):
        file = self.rule('nested', '# Rules\nScope.\n## Parent\nParent restriction.\n### Child\nNeeded.\n```md\n## Fake\n```\n### Sibling\nUnrelated.\n')
        plan = resolve_route_reads({'reads': [{'file': file, 'anchor': '### Child'}]}, self.root)
        self.assertEqual(plan[0]['end_line'], 9)
        self.assertIn('Parent restriction.', plan[0]['text'])
        self.assertIn('## Fake', plan[0]['text'])
        self.assertNotIn('Unrelated.', plan[0]['text'])

    def test_simultaneous_hits_merge_overlapping_reads(self):
        file = self.rule('overlap', '# Rules\nScope.\n## Parent\nNeeded.\n### Child\nAlso needed.\n## Other\nNot needed.\n')
        hits = [{'mechanism': anchor, 'reads': [{'file': file, 'anchor': anchor}]}
                for anchor in ('## Parent', '### Child', '## Parent')]
        context = build_additional_context(hits, self.root)
        self.assertEqual(context.count(file), 1)
        self.assertIn(file + ':1-6', context)

    def test_bad_or_ambiguous_anchor_falls_back_visibly(self):
        file = self.rule('ambiguous', '# Rules\n## Repeat\nOne.\n## Repeat\nTwo.\n')
        for anchor in ('## Absent', '## Repeat'):
            with self.subTest(anchor=anchor):
                route = {'mechanism': 'test', 'reads': [{'file': file, 'anchor': anchor}]}
                plan = resolve_route_reads(route, self.root)
                self.assertEqual(plan[0]['end_line'], 5)
                self.assertIn('Two.', plan[0]['text'])
                self.assertIsNotNone(plan[0]['fallback'])
                self.assertIn('回退全文', build_additional_context([route], self.root))

    def test_unreadable_dependency_is_not_successful_empty_read(self):
        route = {'mechanism': 'test', 'reads': [{'file': '.system/rules/missing.md', 'anchor': None}]}
        plan = resolve_route_reads(route, self.root)
        self.assertTrue(plan[0]['missing'])
        self.assertIsNone(plan[0]['start_line'])
        self.assertIn('读取未完成', build_additional_context([route], self.root))

    def test_rule_path_cannot_escape_control_plane(self):
        for file in ('../private.md', '.system/rules/../../private.md'):
            with self.subTest(file=file), self.assertRaises(ValueError):
                resolve_route_reads({'reads': [{'file': file, 'anchor': None}]}, self.root)
        outside = self.root / 'private.md'
        outside.write_text('private', encoding='utf-8')
        (self.root / '.system/rules/link.md').symlink_to(outside)
        with self.assertRaises(ValueError):
            resolve_route_reads({'reads': [{'file': '.system/rules/link.md', 'anchor': None}]}, self.root)

    def test_schema_rejects_old_contract_and_invalid_anchor_types(self):
        schema = load_schema('route_map')
        base = {'_meta': {'purpose': 'test', 'source_of_truth': 'test'}, 'routes': []}
        for anchor in (None, '## Section'):
            base['routes'] = [{'mechanism': 'test', 'keywords': ['test'], 'reads': [
                {'file': '.system/rules/test.md', 'anchor': anchor}]}]
            self.assertEqual(validate(base, schema), [])
        for anchor in (True, 3, [], ''):
            base['routes'][0]['reads'][0]['anchor'] = anchor
            self.assertTrue(validate(base, schema))
        base['routes'] = [{'mechanism': 'test', 'keywords': ['test'], 'files': ['test.md'], 'anchor': '# Test'}]
        self.assertTrue(validate(base, schema))

    def test_nullable_type_does_not_accept_boolean_as_number(self):
        self.assertTrue(validate(True, {'type': ['integer', 'null']}))
        self.assertEqual(validate(None, {'type': ['integer', 'null']}), [])

    def test_live_read_contract_has_no_missing_dependencies(self):
        self.assertEqual(check_route_map_integrity(Path(__file__).resolve().parents[2]), [])

    def test_exact_and_substring_matching_are_unchanged(self):
        routes = load_routes()
        self.assertIn('审计', {r['mechanism'] for r in match_prompt('对当前方案做一次审计', routes)})
        self.assertEqual(match_prompt('今天气温怎么样', routes), [])
        exact = [{'mechanism': 'tip', 'match_type': 'exact', 'keywords': ['tip']}]
        self.assertEqual(match_prompt(' TIP ', exact), exact)
        self.assertEqual(match_prompt('tips for you', exact), [])

    def test_no_hits_emit_no_loading_instructions(self):
        self.assertEqual(build_additional_context([], self.root), '')


if __name__ == '__main__':
    unittest.main()
