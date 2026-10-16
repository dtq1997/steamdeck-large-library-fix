"""[Codex] Compatibility across renaming and layout changes, with unsafe-change controls."""
import json
import os
import re
import subprocess
import unittest

import compat
import structural

FRAGMENT = 'function result(value){return helper(value)+ns.offset}'
CONTEXT = '(module,exports,load)=>{const ns=load(7);function helper(value){return value*2}' + FRAGMENT + '}'
COUNT = ('function label({collection:n}){return [n?.visibleApps.filter(f.collectionsAppFilter.Matches).length??0,'
         'n?.GetAppCountWithToolsFilter(f.collectionsAppFilter),styles.CollectionLabelCount]}')
PREVIEW = ('function preview(arg){let apps=arg.collection.visibleApps;'
           'apps=apps.slice().sort((a,b)=>b.minutes_playtime_forever-a.minutes_playtime_forever).slice(0,13);return apps}')


def execute(source):
    runner = os.environ.get('LLF_DEVELOPMENT_QJS', str(compat.HERE / 'qjs-linux-x86_64'))
    call = subprocess.run([runner, '--std', '-I', str(compat.HERE / 'operations.js'), '-e', source],
                          capture_output=True, text=True, timeout=10, env={'LANG': 'C.UTF-8'})
    if call.returncode:
        raise AssertionError(call.stderr)
    return json.loads(call.stdout)


class StructuralTests(unittest.TestCase):
    def certificate(self, context=CONTEXT, fragment=FRAGMENT, outer=()):
        return compat.run_syntax([{'structural': {'context': context, 'source': fragment,
            'start': context.index(fragment), 'outer_bindings': list(outer)}}])['results'][0]

    def operations(self, source):
        context = '(m,e,r)=>{' + source + '}'
        result = compat.run_syntax([{'operation_edits': {'context': context}}])['results'][0]
        self.assertNotIn('error', result)
        return context, result['operations']

    def transformed(self, source, feature):
        context, operations = self.operations(source)
        edits = [edit for item in operations if item['feature'] == feature for edit in item['edits']]
        self.assertTrue(edits)
        for start, end, replacement in sorted(compat.spelling_edits(context, edits), reverse=True):
            context = context[:start] + replacement + context[end:]
        return context

    def test_closed_dependencies_survive_renaming_and_unrelated_helpers(self):
        base = self.certificate()
        changed, fragment = CONTEXT, FRAGMENT
        for old, new in [('load', 'require'), ('ns', 'namespace'), ('helper', 'convert'), ('result', 'answer')]:
            changed = re.sub(r'\b' + old + r'\b', new, changed)
            fragment = re.sub(r'\b' + old + r'\b', new, fragment)
        changed = changed[:-1] + 'function unrelated(){return 99}}'
        self.assertEqual(self.certificate(changed, fragment)['canonical'], base['canonical'])
        for replacement in ['load(8)', 'load(7).other']:
            self.assertNotEqual(self.certificate(CONTEXT.replace('load(7)', replacement))['canonical'], base['canonical'])

    def test_dependency_writes_shadowing_and_owner_inheritance_are_guarded(self):
        base = self.certificate()['canonical']
        changed = CONTEXT[:-1] + 'helper=other;}'
        self.assertNotEqual(self.certificate(changed)['canonical'], base)
        self.assertIn('error', self.certificate(CONTEXT[:-1] + 'function mutate(){helper=other}}'))
        self.assertIn('error', self.certificate(CONTEXT[:-1] + 'ns.offset=99;}'))
        self.assertIn('error', self.certificate(CONTEXT.replace('return value*2', 'return value*outside'), outer=['outside']))
        method = 'read(){return this.value}'
        a = '(m,e,r)=>{const Base=r(7);class Owner extends Base{' + method + '}}'
        b = a.replace('r(7)', 'r(8)')
        self.assertNotEqual(self.certificate(a, method)['canonical'], self.certificate(b, method)['canonical'])

    def test_replacement_renaming_avoids_capturing_new_client_aliases(self):
        patch = 'function result(value){const local=value+1;return helper(local)+ns.offset}'
        actual = re.sub(r'\bns\b', 'local', CONTEXT.replace('helper', '__llf0'))
        result = compat.run_syntax([{'adapt_patch': {'source': patch, 'context': actual,
            'names': [['result', 'answer'], ['helper', '__llf0'], ['ns', 'local']], 'named': [['result', 'answer']]}}])['results'][0]
        self.assertNotIn('error', result)
        code = 'const local={offset:3};function __llf0(x){return x*2}' + result['replacement']
        self.assertEqual(execute(code + ';print(JSON.stringify(answer(4)))'), 13)

    def test_count_null_guards_cached_zero_and_cold_snapshot_are_preserved(self):
        output = self.transformed(COUNT, 'count')
        code = 'const styles={CollectionLabelCount:1};let calls=0;const f={collectionsAppFilter:{'
        code += 'Matches(){throw Error("old slow path")},ToStorageFormat(){return {limit:3}},'
        code += 'MatchesImpl(app){calls++;return app<this.m_filterSpec.limit}}};'
        code += 'const label=' + output + ';'
        code += 'const get=c=>label()( {collection:c} )[0];'
        code += 'print(JSON.stringify([get(null),get({llfN:0,GetAppCountWithToolsFilter(){}}),'
        code += 'get({visibleApps:[1,4,2],GetAppCountWithToolsFilter(){}}),calls]));'
        # Return the trusted fixture's function from its factory.
        code = code.replace(output + ';', output[:-1] + 'return label};')
        self.assertEqual(execute(code), [0, 0, 2, 3])

    def test_preview_uses_cached_top13_and_keeps_original_order_fallback(self):
        output = self.transformed(PREVIEW, 'preview')
        factory = output[:-1] + 'return preview}'
        code = 'const preview=(' + factory + ')();const apps=[1,3,2].map(x=>({minutes_playtime_forever:x}));'
        code += 'const cached=[{minutes_playtime_forever:9}];print(JSON.stringify(['
        code += 'preview({collection:{visibleApps:apps}}).map(x=>x.minutes_playtime_forever),'
        code += 'preview({collection:{visibleApps:apps,llfP:cached}})===cached,apps.map(x=>x.minutes_playtime_forever)]));'
        self.assertEqual(execute(code), [[3, 2, 1], True, [1, 3, 2]])
        changed = PREVIEW.replace('-a.minutes_playtime_forever', '+a.minutes_playtime_forever')
        self.assertFalse(self.operations(changed)[1])

    def test_discovery_bounds_ambiguity_and_parser_rejects_invalid_positions(self):
        tokens = compat.run_syntax([{'structural_tokens': FRAGMENT}])['results'][0]['tokens']
        entry = {'tokens': tokens, 'scope': '123', 'kind': 'methods', 'index': 0}
        rows, _ = structural.discover('const modules={456:(m,e,r)=>{' + FRAGMENT + '}};', {'structural': [entry]})
        self.assertEqual(len(rows), 1)
        rows, _ = structural.discover(FRAGMENT * 65, {'structural': [entry]})
        self.assertEqual(rows, [])
        with self.assertRaises(compat.Incompatible):
            compat.run_native('const a=1;', [], [], factories=[-1])

    def test_theme_changes_watch_only_theme_nodes_and_rollback_on_failure(self):
        source = r'''
let observations=[],disconnects=0,callback,hide,active=true,fail=false;
const classes=new Set(),child={},root={children:[child]};
const selector=':root:has(.Root > div[data-accent-color="blue"]) .Button';
const rule={selectorText:selector,cssRules:[]};
const doc={styleSheets:[{cssRules:[{cssRules:[rule]}]}],querySelectorAll:s=>s==='.Root'?[root]:[],
 documentElement:{matches:s=>active,classList:{toggle:(k,on)=>on?classes.add(k):classes.delete(k),remove:k=>classes.delete(k)}},
 defaultView:{MutationObserver:class{constructor(fn){callback=fn}disconnect(){disconnects++;observations=[]}
 observe(node,options){observations.push([node===root?'root':node===child?'child':'unknown',options]);if(fail)throw Error('observe failure')}},
 addEventListener:(name,fn)=>{if(name==='pagehide')hide=fn}}};
themeDocument(doc);
const initialized=rule.selectorText,watched=observations;
active=false;callback([{type:'attributes'}]);const cleared=classes.size===0;
active=true;callback([{type:'childList'}]);const rebound=classes.size===1;
hide();const stopped=observations.length===0;
delete doc.__llfThemeState;rule.selectorText=selector;fail=true;themeDocument(doc);
print(JSON.stringify({initialized,watched,cleared,rebound,stopped,rolledBack:rule.selectorText===selector,
 disconnectedOnFailure:observations.length===0,noStaleState:!doc.__llfThemeState}));
'''
        result = execute(source)
        self.assertIn(':root.llfTheme0.llfTheme0:not(llfNever)', result['initialized'])
        self.assertEqual([row[0] for row in result['watched']], ['root', 'child'])
        self.assertEqual(result['watched'][0][1], {'childList': True})
        self.assertEqual(result['watched'][1][1]['attributeFilter'],
                         ['data-accent-color', 'data-dull-color', 'data-body-text-color'])
        self.assertTrue(all(result[key] for key in ['cleared', 'rebound', 'stopped', 'rolledBack', 'disconnectedOnFailure', 'noStaleState']))


if __name__ == '__main__':
    unittest.main()
