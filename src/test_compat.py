"""[Codex] Compatibility, transaction and optional real-client fixture checks."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import compat
import engine

HERE = Path(__file__).resolve().parent
RULES = json.loads((HERE / 'rules.json').read_text())
META = json.loads((HERE / 'compatibility.json').read_text())
FIXTURES = os.environ.get('LLF_FIXTURE_DIR')


class SyntaxTests(unittest.TestCase):
    def canonical(self, text):
        return compat.run_syntax([text])['results'][0]['canonical']

    def test_local_names_and_comments(self):
        a = self.canonical('f(a){let b=a+1;return b}')
        b = self.canonical('f(input) { /* comment */ let result=input+1; return result; }')
        self.assertEqual(a, b)

    def test_dependencies_exclude_parameters_locals_and_property_names(self):
        text = 'f(input){const local=1;return dependency.read(input)+object[computed]+local}'
        result = compat.run_syntax([text])['results'][0]
        self.assertEqual(result['identifiers'], ['computed', 'dependency', 'object'])

    def test_automatic_semicolon_and_shadowing_are_preserved(self):
        self.assertNotEqual(self.canonical('f(){return\n1}'), self.canonical('f(){return 1}'))
        self.assertNotEqual(self.canonical('f(a){return new Set(a)}'), self.canonical('f(Set){return new Set()}'))

    def test_only_related_header_bindings(self):
        values = compat.run_syntax([
            {'header_prefix': 'var h=t(1),stamp="old",alias=h;function next(){}', 'bindings': ['alias']},
            {'header_prefix': 'var h=t(1),stamp="new",unrelated=t(99),alias=h;function next(){}', 'bindings': ['alias']},
            {'header_prefix': 'var h=t(2),stamp="new",alias=h;function next(){}', 'bindings': ['alias']},
        ])['results']
        self.assertEqual(values[0]['canonical'], values[1]['canonical'])
        self.assertNotEqual(values[0]['canonical'], values[2]['canonical'])

    def test_css_only_ignores_eof_comments(self):
        self.assertEqual(compat.css_digest(b'a{color:red}'), compat.css_digest(b'a{color:red}\n/* comment */'))
        self.assertNotEqual(compat.css_digest(b'a{color:red}'), compat.css_digest(b'a{color:blue}'))

    def test_spelling_budget_preserves_values_directives_and_unicode_offsets(self):
        source = 'const before="😀";function f(){"use\\x20strict";return "\\u6e38\\u620f"}'
        result = compat.run_syntax([], source, collect=True)
        edits = compat.spelling_edits(source, result['spellings'])
        self.assertTrue(edits)
        output = source
        for start, end, replacement in sorted(edits, reverse=True):
            self.assertEqual(self.canonical(source[start:end]), self.canonical(replacement))
            output = output[:start] + replacement + output[end:]
        self.assertIn('"use\\x20strict"', output)
        self.assertIn('"游戏"', output)
        self.assertEqual(self.canonical(source), self.canonical(output))

    def test_metadata_and_vendor_integrity(self):
        self.assertEqual(META['version'], engine.VERSION)
        for profile in [RULES, *RULES['alternates']]:
            meta = META['profiles'][profile['tested_client']]
            covered = [i for group in meta['groups'] for i in group['methods']]
            self.assertEqual(sorted(covered), list(range(len(profile['methods']))))
            self.assertEqual(len(covered), len(set(covered)))
            self.assertEqual(meta['groups'][0]['contracts'], profile['wake_contracts'])
        for item in json.loads((HERE / 'vendor.json').read_text()):
            self.assertEqual(engine.sha((HERE / item['file']).read_bytes()), item['sha256'])

    def test_parser_failure_is_explicit(self):
        with patch.object(compat.subprocess, 'run', side_effect=OSError('fixture failure')):
            with self.assertRaisesRegex(compat.Incompatible, 'unavailable'):
                compat.run_syntax(['f(){}'])

    def test_native_parser_rejects_matches_inside_noncode_and_invalid_programs(self):
        source = 'const real=1;/* fakeMethod(){} */const a="quoted";const b=`template`;'
        offsets = [source.index(name) for name in ['real', 'fakeMethod', 'quoted', 'template']]
        checked = compat.run_native(source, offsets, [])
        self.assertEqual(checked['locations'], [offsets[0]])
        with self.assertRaises(compat.Incompatible):
            compat.run_native('function broken({', [], [])

    def test_helper_does_not_inherit_credentials(self):
        import subprocess
        response = subprocess.CompletedProcess([], 0, '{"results":[],"locations":[],"spellings":[]}')
        with patch.object(compat.subprocess, 'run', return_value=response) as run:
            compat.run_syntax([])
        self.assertEqual(run.call_args.kwargs['env'], {'LANG': 'C.UTF-8'})
        self.assertEqual(run.call_args.args[0][:3], ['/usr/bin/nice', '-n', '10'])

    def test_busy_is_reported_without_overwriting_status(self):
        with patch.object(engine, 'execute_locked', side_effect=engine.Busy('fixture busy')), \
             patch.object(engine, 'save_json') as save:
            result = engine.execute('status', Path('unused'), Path('unused'), {})
        self.assertEqual(result['status'], 'busy')
        save.assert_not_called()


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.compat-test-', dir=HERE)
        self.root = Path(self.temp.name)
        self.ui = self.root / 'ui'
        self.state = self.root / 'state'
        self.ui.mkdir(); self.state.mkdir()
        self.original, self.patched = b'original', b'patched'
        self.record = {'file': 'sp.js', 'original_sha256': engine.sha(self.original),
                       'patched_sha256': engine.sha(self.patched), 'tested_client': 'fixture'}
        backup = self.state / 'backups' / (self.record['original_sha256'] + '.js')
        backup.parent.mkdir(); backup.write_bytes(self.original)
        engine.save_json(self.state / 'pending.json', {'record': self.record, 'before': engine.sha(self.original)})

    def tearDown(self):
        self.temp.cleanup()

    def test_interrupted_after_file_write_finalizes_receipt(self):
        (self.ui / 'sp.js').write_bytes(self.patched)
        engine.recover_pending(self.ui, self.state)
        self.assertEqual(engine.read_receipt(self.state, 'sp.js'), self.record)
        self.assertFalse((self.state / 'pending.json').exists())

    def test_interrupted_before_file_write_leaves_original(self):
        (self.ui / 'sp.js').write_bytes(self.original)
        engine.recover_pending(self.ui, self.state)
        self.assertFalse((self.state / 'pending.json').exists())
        self.assertEqual((self.ui / 'sp.js').read_bytes(), self.original)

    def test_later_steam_update_is_preserved_and_old_journal_archived(self):
        (self.ui / 'sp.js').write_bytes(b'new Steam update')
        engine.recover_pending(self.ui, self.state)
        self.assertEqual((self.ui / 'sp.js').read_bytes(), b'new Steam update')
        self.assertEqual(len(list((self.state / 'interrupted').glob('*.json'))), 1)
        self.assertFalse((self.state / 'pending.json').exists())

    def test_source_race_never_overwrites_later_content(self):
        target = self.ui / 'sp.js'; target.write_bytes(b'new Steam update')
        with self.assertRaisesRegex(engine.Unsupported, 'Source changed'):
            engine.atomic_write(target, self.patched, expected=engine.sha(self.original))
        self.assertEqual(target.read_bytes(), b'new Steam update')

    def test_dense_edits_preserve_original_offsets_unicode_and_adjacent_ranges(self):
        text = 'const x="😀";\r\n' + 'abcde' * 2000
        changes = [(index, index + 1, 'Q') for index in range(20, len(text) - 2, 13)]
        changes.extend([(0, 5, 'CONST'), (5, 6, ' ')])
        expected = text
        for start, end, replacement in sorted(changes, reverse=True):
            expected = expected[:start] + replacement + expected[end:]
        plan = {'changes': changes, 'syntax_checked': False}
        self.assertEqual(engine.build(text.encode(), {'_plan': plan}), expected.encode())
        for invalid in [[(0, 3, 'x'), (2, 4, 'y')], [(-1, 1, '')], [(1, 1, '')], [(0, len(text) + 1, '')]]:
            with self.subTest(invalid=invalid), self.assertRaises(engine.Unsupported):
                engine.build(text.encode(), {'_plan': {'changes': invalid}})


@unittest.skipUnless(FIXTURES, 'Set LLF_FIXTURE_DIR to private saved client fixtures')
class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = Path(FIXTURES)
        cls.original = (cls.fixture / 'new-client.original.js').read_bytes()
        cls.css = (cls.fixture / 'sp.original.css').read_bytes()
        cls.profile = RULES['alternates'][0]

    def plan(self, data, css=None):
        plan = engine.compatible_plan(data, self.profile, self.css if css is None else css)
        return plan, engine.build(data, {**self.profile, '_plan': plan})

    def test_both_native_clients_keep_exact_previous_patch(self):
        old = (self.fixture.parent / 'chunk.original.js').read_bytes()
        for data, profile, css in [(old, RULES, None), (self.original, self.profile, self.css)]:
            with patch.object(compat, 'run_syntax', side_effect=AssertionError('Known source must be fast')):
                plan = engine.compatible_plan(data, profile, css)
                output = engine.build(data, {**profile, '_plan': plan})
            self.assertFalse(plan['paused'])
            self.assertEqual(output, engine.build_legacy(data, profile))
        self.assertEqual(engine.sha(self.plan(self.original)[1]),
                         '527dfb2f6476097b1ae4263df81287e48b12af6fe3ad124ba414443c9eb57c43')

    def test_comments_and_locals_keep_all_groups(self):
        start, end = META['profiles'][self.profile['tested_client']]['ranges']['original'][0]
        raw = self.original.decode()[start:end]
        renamed = compat.run_syntax([raw])['results'][0]['canonical']
        for text in [raw.replace('{', '{/* harmless */', 1), renamed]:
            source = self.original.decode(); changed = (source[:start] + text + source[end:]).encode()
            plan, output = self.plan(changed)
            self.assertFalse(plan['paused'])
            self.assertEqual(len(output), len(changed))

    def test_actual_method_change_pauses_wake_only(self):
        changed = self.original.replace(b'UpdateAppOverview(V){', b'UpdateAppOverview(V){this.futureRequiredStep();', 1)
        plan, output = self.plan(changed)
        self.assertEqual(set(plan['paused']), {'wake'})
        self.assertIn(b'this.futureRequiredStep()', output)
        self.assertEqual(set(plan['active']), {'startup', 'library', 'navigation'})

    def test_trailing_comments_keep_groups_but_extra_code_is_rejected(self):
        changed = self.original + b'\n/* harmless */\n// harmless too\n'
        plan, output = self.plan(changed)
        self.assertFalse(plan['paused'])
        self.assertEqual(len(output), len(changed))
        with self.assertRaises(engine.Unsupported):
            self.plan(self.original + b'\nfutureUnknownCode();')

    def test_native_model_change_pauses_wake(self):
        changed = self.original.replace(b'BHasObservableChange(V){', b'BHasObservableChange(V){this.futureRequiredStep();', 1)
        plan, output = self.plan(changed)
        self.assertIn('wake', plan['paused'])
        self.assertIn(b'this.futureRequiredStep()', output)

    def test_unrelated_build_stamp_changes_are_tolerated(self):
        changed = self.original.replace(b'11123049', b'99999999')
        self.assertNotEqual(changed, self.original)
        plan, _ = self.plan(changed)
        self.assertNotIn('wake', plan['paused'])
        self.assertNotIn('startup', plan['paused'])
        self.assertNotIn('library', plan['paused'])

    def test_css_changes_do_not_disable_wake_or_library(self):
        for css in [self.css + b'\n/* harmless */', self.css + b'\n.future{color:red}']:
            plan, _ = self.plan(self.original, css)
            self.assertIn('wake', plan['active']); self.assertIn('library', plan['active'])
        self.assertIn('navigation', plan['paused'])

    def test_incomplete_file_and_parser_failure_never_patch(self):
        with self.assertRaises(engine.Unsupported):
            self.plan(self.original[:-240])
        changed = self.original.replace(b'UpdateAppOverview(V){', b'UpdateAppOverview(V){/* comment */', 1)
        with patch.object(compat, 'run_syntax', side_effect=compat.Incompatible('fixture failure')):
            with self.assertRaises(engine.Unsupported):
                self.plan(changed)

    def test_older_client_budget_never_disables_other_working_groups(self):
        data = (self.fixture.parent / 'chunk.original.js').read_bytes()
        data = data.replace(b'UpdateApps(e,t){const r=new Set(this.m_setApps);',
                            b'UpdateApps(e,t){this.futureRequiredStep();const r=new Set(this.m_setApps);', 1)
        plan = engine.compatible_plan(data, RULES)
        output = engine.build(data, {**RULES, '_plan': plan})
        self.assertIn('wake', plan['active'])
        self.assertIn('library', plan['paused'])
        self.assertEqual(len(output), len(data))

    def sandbox(self, root):
        ui, state = root / 'ui', root / 'state'
        (ui / 'css').mkdir(parents=True)
        (ui / 'library.js').write_text('current entry;3714:"sp"')
        (ui / 'sp.js').write_bytes(self.original)
        (ui / 'css/sp.css').write_bytes(self.css)
        return ui, state

    def test_local_only_update_installs_without_requiring_an_old_patch_backup(self):
        source = (self.fixture.parent / 'chunk.original.js').read_bytes()
        source = source.replace(b'UpdateAppOverview(e){', b'UpdateAppOverview(e){this.futureWakeStep();', 1)
        source = source.replace(b'UpdateApps(e,t){const r=new Set(this.m_setApps);',
                                b'UpdateApps(e,t){this.futureCollectionStep();const r=new Set(this.m_setApps);', 1)
        source = source.replace(b'async InitAfterLogin(){',
                                b'async InitAfterLogin(){this.futureLoginStep();const futureSpace="' + b'\\u6e38' * 100 +
                                b'"+"' + b'\\u6e38' * 100 + b'";', 1)
        with tempfile.TemporaryDirectory(prefix='.compat-test-', dir=HERE) as name:
            ui, state = Path(name) / 'ui', Path(name) / 'state'
            ui.mkdir()
            (ui / 'library.js').write_text('fixture')
            target = ui / 'chunk~fixture.js'
            target.write_bytes(source)
            result = engine.execute('apply', ui, state, RULES)
            self.assertEqual(result['status'], 'partial', result)
            self.assertEqual(set(result['active']), {'startup', 'collections'})
            output = target.read_bytes()
            self.assertEqual(len(output), len(source))
            for call in [b'futureWakeStep()', b'futureCollectionStep()', b'futureLoginStep()']:
                self.assertIn(call, output)
            with patch.object(engine, 'compatible_plan', side_effect=AssertionError('Cache was not reused')):
                self.assertEqual(engine.execute('status', ui, state, RULES)['status'], 'partial')
            receipt = engine.read_receipt(state, target.name)
            cache = state / 'plans' / (receipt['plan_cache'] + '.json')
            cache.write_text('{broken')
            self.assertEqual(engine.execute('apply', ui, state, RULES)['status'], 'partial')
            self.assertEqual(engine.execute('restore', ui, state, RULES)['status'], 'restored')
            self.assertEqual(target.read_bytes(), source)

    def test_partial_apply_recovers_full_and_new_update_uninstalls_to_new_original(self):
        with tempfile.TemporaryDirectory(prefix='.compat-test-', dir=HERE) as name:
            ui, state = self.sandbox(Path(name))
            (ui / 'css/sp.css').write_bytes(self.css + b'\n.future{color:red}')
            partial = engine.execute('apply', ui, state, RULES)
            self.assertEqual(partial['status'], 'partial')
            self.assertIn('navigation', partial['paused'])
            self.assertEqual(engine.execute('status', ui, state, RULES)['status'], 'partial')
            (ui / 'css/sp.css').write_bytes(self.css)
            self.assertEqual(engine.execute('apply', ui, state, RULES)['status'], 'applied')
            self.assertEqual(engine.sha((ui / 'sp.js').read_bytes()),
                             '527dfb2f6476097b1ae4263df81287e48b12af6fe3ad124ba414443c9eb57c43')
            updated = self.original.replace(b'UpdateAppOverview(V){', b'UpdateAppOverview(V){/* update */', 1)
            (ui / 'sp.js').write_bytes(updated)
            self.assertEqual(engine.execute('apply', ui, state, RULES)['status'], 'applied')
            self.assertEqual(engine.execute('restore', ui, state, RULES)['status'], 'restored')
            self.assertEqual((ui / 'sp.js').read_bytes(), updated)

    def test_layout_race_defers_without_writing_then_retries_compatible_groups(self):
        with tempfile.TemporaryDirectory(prefix='.compat-test-', dir=HERE) as name:
            ui, state = self.sandbox(Path(name))
            original_build = engine.build
            def change_layout(data, profile):
                result = original_build(data, profile)
                (ui / 'css/sp.css').write_bytes(self.css + b'\n.future{color:red}')
                return result
            with patch.object(engine, 'build', side_effect=change_layout):
                self.assertEqual(engine.execute('apply', ui, state, RULES)['status'], 'deferred')
            self.assertEqual((ui / 'sp.js').read_bytes(), self.original)
            self.assertEqual(engine.execute('apply', ui, state, RULES)['status'], 'partial')


if __name__ == '__main__':
    unittest.main()
