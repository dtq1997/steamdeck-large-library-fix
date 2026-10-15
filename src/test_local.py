"""[Codex] Local rewrites: preserve new logic, reject ambiguous or shadowed code."""
import unittest

import compat


COLLECTION = ('UpdateApps(items,removed){this.futureStep();return '
              'JSON.stringify(Array.from(items.values()).sort())!='
              'JSON.stringify(Array.from(this.m_setApps.values()).sort())}')
STARTUP = 'async InitAfterLogin(){this.futureStep();const jobs=[["DynamicUserStore",()=>store.Fm.Get().HintLoad()]];await this.run(jobs)}'


class LocalTests(unittest.TestCase):
    def transform(self, method, before='', after='', parameters='module,exports,require'):
        context = '(' + parameters + ')=>{' + before + 'class C{' + method + '}' + after + '}'
        return compat.run_syntax([{'local_unit': {'source': method, 'context': context}}])['results'][0]

    def test_local_edits_preserve_surrounding_new_calls_and_identifiers(self):
        for method, feature in [(COLLECTION, 'collections'), (STARTUP, 'startup')]:
            with self.subTest(feature=feature):
                result = self.transform(method)
                self.assertEqual(result['feature'], feature)
                edits = compat.spelling_edits(method, result['edits'])
                self.assertEqual(len(edits), 1)
                a, b, replacement = edits[0]
                output = method[:a] + replacement + method[b:]
                self.assertIn('this.futureStep();', output)
                if feature == 'startup':
                    self.assertIn('this.run(jobs)', output)
                self.assertFalse(self.transform(output).get('edits'))

    def test_changed_operation_and_duplicate_targets_are_refused(self):
        cases = [COLLECTION.replace('!=JSON', '==JSON'),
                 COLLECTION.replace('.sort())!=', '.sort(this.order))!='),
                 COLLECTION.replace('this.m_setApps', 'this.otherSet'),
                 COLLECTION.replace('return ', COLLECTION.split('return ')[1][:-1] + ';return '),
                 STARTUP.replace('DynamicUserStore', 'OtherStore'),
                 STARTUP.replace('HintLoad()', 'HintLoad(true)')]
        for method in cases:
            with self.subTest(method=method[:80]):
                self.assertFalse(self.transform(method).get('edits'))

    def test_builtin_shadowing_eval_and_with_are_refused(self):
        for method, name in [(COLLECTION, 'JSON'), (COLLECTION, 'Array'),
                             (STARTUP, 'Promise'), (STARTUP, 'setTimeout'), (STARTUP, 'clearTimeout')]:
            with self.subTest(name=name):
                self.assertFalse(self.transform(method, before='let ' + name + '=other;').get('edits'))
                self.assertFalse(self.transform(method, parameters=name + ',e,r').get('edits'))
                self.assertFalse(self.transform(method.replace('{', '{let ' + name + '=other;', 1)).get('edits'))
        self.assertFalse(self.transform(COLLECTION, before='eval(source);').get('edits'))

    def test_native_locator_ignores_decoys_and_reports_outer_bindings(self):
        method = COLLECTION.replace('items', 'renamed')
        source = ('(()=>{const JSON=custom;const modules={98765:(a,b,c)=>{class C{' + method +
                  '}}};const text="UpdateApps(fake){}";/* InitAfterLogin(){} */})();')
        positions = [source.index('UpdateApps'), source.index('UpdateApps(fake)'), source.index('InitAfterLogin')]
        result = compat.run_native(source, positions, [], positions)
        self.assertEqual(len(result['units']), 1)
        unit = result['units'][0]
        self.assertIn('JSON', unit['outer_bindings'])
        self.assertEqual(source[unit['start']:unit['end']], method)
        context = source[unit['context_start']:unit['context_end']]
        checked = compat.run_syntax([{'local_unit': {'source': method, 'context': context,
                                                   'outer_bindings': unit['outer_bindings']}}])['results'][0]
        self.assertFalse(checked.get('edits'))

    def test_string_space_is_value_preserving_and_never_creates_directives(self):
        import json
        quoted = json.dumps('say "hello", "world"')
        source = 'function f(){"use\\u0020strict";return ' + quoted + '}const text="\\u6e38\\u620f";'
        candidates = compat.budget_candidates(source, {})
        self.assertTrue(candidates)
        offsets = compat.byte_offsets(source, [p for item in candidates for p in (item['start'], item['start'] + len(item['raw']))])
        budgets = [{**item, 'start': offsets[item['start']], 'end': offsets[item['start'] + len(item['raw'])]} for item in candidates]
        native = compat.run_native(source, [], budgets)
        accepted = [candidates[index] for index in native['budgets']]
        self.assertTrue(accepted)
        self.assertFalse(any(item['raw'] == '"use\\u0020strict"' for item in accepted))
        values = compat.run_syntax([{'literal_pair': [item['raw'], item['replacement']]} for item in accepted])['results']
        self.assertTrue(all(value.get('equal') for value in values))
        self.assertEqual(compat.budget_candidates('const text="\\ud800";', {}), [])

    def test_outer_parenthesized_eval_is_guarded_without_reading_function_bodies(self):
        source = '(()=>{(eval)(input);const modules={12:(a,b,r)=>{class C{' + COLLECTION + '}}}})()'
        position = source.index('UpdateApps')
        result = compat.run_native(source, [position], [], [position])
        self.assertEqual(len(result['units']), 1)
        self.assertIn('Array', result['units'][0]['outer_bindings'])


if __name__ == '__main__':
    unittest.main()
