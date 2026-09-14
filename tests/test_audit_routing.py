import contextlib
import io
import json
import tempfile
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch

from tools import audit_routing as AR
from tools import hook_route_match as HRM


class CorpusTests(TestCase):
    def test_registered_regression_still_passes(self):
        coverage = AR.audit_coverage(AR.load_routes(), AR.load_cases(), 'regression')
        self.assertEqual(AR.regression_failures(coverage), [])
        self.assertEqual(coverage['untested_mechanisms'], [])

    def test_holdout_reports_both_misses_and_extra_hits_separately(self):
        routes = [{'mechanism': '审计', 'keywords': ['审计']}]
        regression = [{'kind': 'positive', 'instruction': '审计', 'expect': ['审计']}]
        holdout = [
            {'kind': 'semantic', 'instruction': '别审计', 'expect': []},
            {'kind': 'semantic', 'instruction': '核查现有方案', 'expect': ['审计']},
        ]
        coverage = AR.audit_corpora(routes, regression, holdout)
        self.assertEqual(coverage['regression']['summary']['positive']['total'], 1)
        self.assertEqual(coverage['holdout']['summary']['semantic'], {'total': 2, 'clean': 0, 'missed': 1, 'extra': 1})
        self.assertEqual(AR.regression_failures(coverage['regression']), [])

    def test_strict_exit_depends_on_regression_not_holdout(self):
        routes = AR.load_routes()
        holdout = [{'kind': 'semantic', 'instruction': '不要系统自检', 'expect': []}]
        for expected, exit_code in ((['系统自检'], 0), ([], 1)):
            regression = [{'kind': 'positive', 'instruction': '系统自检', 'expect': expected}]
            with self.subTest(expected=expected), patch.object(AR, 'load_routes', return_value=routes), \
                    patch.object(AR, 'load_cases', return_value=regression), \
                    patch.object(AR, 'load_holdout_cases', return_value=holdout), \
                    patch('sys.argv', ['audit_routing.py', '--strict', '--json']), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(AR.main(), exit_code)
                report = json.loads(output.getvalue())
                self.assertEqual(report['coverage']['holdout']['summary']['semantic']['extra'], 1)

    def test_frozen_holdout_is_disjoint_from_regression(self):
        regression = {c['instruction'] for c in AR.load_cases()}
        holdout = AR.load_holdout_cases()
        self.assertEqual(regression.intersection(c['instruction'] for c in holdout), set())
        # The corpus is a diagnostic, so at least these genuinely different categories must remain represented.
        self.assertTrue({'negation', 'quotation', 'multiple_actions', 'paraphrase', 'domain'} <= {c['category'] for c in holdout})


class CostTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.entropaxis/rules').mkdir(parents=True)
        for file in AR.RESIDENT_FILES:
            (self.root / file).write_text('Resident.\n', encoding='utf-8')

    def rule(self, name, text):
        file = '.entropaxis/rules/' + name + '.md'
        (self.root / file).write_text(text, encoding='utf-8')
        return file

    def test_full_hook_and_overlapping_dependencies_are_counted_once(self):
        text = '# Rules\nScope.\n## Main\nNeeded.\n### Nested\nNested text.\n'
        file = self.rule('first', text)
        dependency_text = '# Dependency\nRequired policy.\n'
        dependency = self.rule('dependency', dependency_text)
        route = {'mechanism': 'test', 'reads': [
            {'file': file, 'anchor': '## Main'},
            {'file': file, 'anchor': '### Nested'},
            {'file': file, 'anchor': '## Main'},
            {'file': dependency, 'anchor': None},
        ]}
        cost = AR.audit_cost([route], self.root)['scenarios'][0]
        read_cost = AR.estimate_tokens(text) + AR.estimate_tokens(dependency_text)
        self.assertEqual(cost['necessary_read_ranges']['estimated_tokens'], read_cost)
        self.assertEqual(cost['full_file_baseline']['estimated_tokens'], read_cost)
        hook_text = HRM.build_additional_context([route], self.root)
        self.assertEqual(cost['static_context_estimate']['estimated_tokens'], read_cost + AR.estimate_tokens(hook_text))
        self.assertEqual(cost['actual_hook_context']['text'], hook_text)

    def test_missing_dependency_and_resident_do_not_become_zero_cost(self):
        file = self.rule('first', '# Rules\nAvailable.\n')
        route = {'mechanism': 'test', 'reads': [
            {'file': file, 'anchor': None},
            {'file': '.entropaxis/rules/missing.md', 'anchor': None},
        ]}
        (self.root / AR.RESIDENT_FILES[0]).unlink()
        cost = AR.audit_cost([route], self.root)
        scenario = cost['scenarios'][0]
        self.assertIsNone(scenario['static_context_estimate']['estimated_tokens'])
        self.assertEqual(scenario['necessary_read_ranges']['unknown_files'], ['.entropaxis/rules/missing.md'])
        self.assertIsNone(cost['resident_baseline']['estimated_tokens'])
        self.assertIsNone(cost['actual_cost'])
        self.assertIsNone(cost['actual_usage'])

    def test_bad_anchor_counts_visible_full_fallback(self):
        text = '# Rules\nScope.\n## Present\nNeeded.\n'
        file = self.rule('first', text)
        route = {'mechanism': 'test', 'reads': [{'file': file, 'anchor': '## Missing'}]}
        scenario = AR.audit_cost([route], self.root)['scenarios'][0]
        self.assertEqual(scenario['necessary_read_ranges']['estimated_tokens'], AR.estimate_tokens(text))
        self.assertIsNotNone(scenario['necessary_read_ranges']['ranges'][0]['fallback'])


if __name__ == '__main__':
    main()
