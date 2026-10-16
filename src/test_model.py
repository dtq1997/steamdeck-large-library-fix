"""[Codex] Wake model slices ignore unreachable helpers, retain actual dependencies."""
import unittest
import re

import compat


CONTEXT = '''(module,exports,load)=>{
const kind=load(7),schema={value:{fnCustomSetter:(self,proto)=>self.SetValue(proto)}};
function normalize(value){return value+kind.A}
class Model{
value=new Set;
InitFromProto(proto){for(let key in schema){schema[key].fnCustomSetter(this,proto)}}
SetValue(proto){this.value=normalize(proto.value())}
BHasObservableChange(proto){return this.value!==normalize(proto.value())}
BHasNonObservableChange(proto){return false}
BHasObservables(){return false}
GetPreservedState(){return undefined}
RestorePreservedState(value){}
GetStoreTags(){return []}
Unrelated(){return 1}
}
class Observable extends Model{BHasObservables(){return true}}
}'''

WAKE = '''class Store{
UpdateAppOverview(data){
const proto=kind.bs.deserializeBinary(data),full=proto.full_update();
let incoming=proto.app_overview(),tags=new Set,changed=false;
for(let item of incoming){let existing=this.m_mapApps.get(item.appid());
let plain=existing?.BHasNonObservableChange(item),observable=existing?.BHasObservableChange(item);
if(!full&&existing&&!plain&&!observable)continue;
existing.InitFromProto(item);changed=true,existing.GetStoreTags().forEach(tag=>tags.add(tag))}
return changed}
}'''


class ModelTests(unittest.TestCase):
    def check(self, context=CONTEXT, outer=()):
        return compat.run_syntax([{'model_context': context, 'outer_bindings': list(outer)}])['results'][0]

    def test_only_reachable_model_operations_form_the_contract(self):
        base = self.check()['model']
        unrelated = CONTEXT.replace('Unrelated(){return 1}', 'Unrelated(){this.futureFlag=true;return 2}get FutureHardware(){return 3}')
        self.assertEqual(self.check(unrelated)['model'], base)
        changed = CONTEXT.replace('this.value=normalize(proto.value())', 'this.value=normalize(proto.value())+1')
        self.assertNotEqual(self.check(changed)['model'], base)

    def test_lexical_aliases_normalize_but_import_identity_is_retained(self):
        base = self.check()['model']
        renamed = CONTEXT
        for before, after in [('schema', 'spec'), ('normalize', 'convert'), ('kind', 'namespace'),
                              ('Model', 'Record'), ('Observable', 'Reactive'), ('load', 'require')]:
            renamed = re.sub(r'\b' + before + r'\b', after, renamed)
        self.assertEqual(self.check(renamed)['model'], base)
        self.assertNotEqual(self.check(CONTEXT.replace('load(7)', 'load(8)'))['model'], base)

    def test_plain_data_extensions_are_parameters_but_special_behavior_is_retained(self):
        base = self.check()['model']
        extension = CONTEXT.replace('schema={value:', 'schema={future_data:{},value:').replace('value=new Set;', 'future_data;value=new Set;')
        self.assertEqual(self.check(extension)['model'], base)
        for altered in [extension.replace('future_data;', 'future_data=sideEffect();'),
                        extension.replace('future_data:{}', 'future_data:{fnCustomSetter:custom}'),
                        extension.replace('future_data;', 'future_data;future_data;'),
                        extension.replace('future_data:{}', 'future_data:{},future_data:{}'),
                        extension.replace('return false', 'return !!this.future_data', 1)]:
            with self.subTest(altered=altered[:60]):
                result = self.check(altered)
                self.assertTrue('error' in result or result['model'] != base)

    def test_schema_escape_and_dependency_writes_are_refused(self):
        for addition in ['sink(schema);', 'schema={};', 'normalize=other;', 'Model.prototype.SetValue=other;']:
            with self.subTest(addition=addition):
                result = self.check(CONTEXT[:-1] + addition + '}')
                self.assertIn('error', result)

    def test_constructor_fields_reflection_and_outer_shadowing_are_guarded(self):
        base = self.check()['model']
        self.assertNotEqual(self.check(CONTEXT.replace('value=new Set;', 'value=new Map;'))['model'], base)
        reflective = CONTEXT.replace('this.value=normalize(proto.value())', 'this.value=normalize.name')
        self.assertIn('error', self.check(reflective))
        self.assertIn('error', self.check(outer=['Set']))

    def test_joint_wake_contract_tracks_aliases_and_rewrites_actual_symbols(self):
        context = CONTEXT[:-1] + WAKE + '}'
        renamed = context
        for before, after in [('kind', 'namespace'), ('schema', 'spec'), ('Model', 'Entry'),
                              ('normalize', 'convert'), ('full', 'all'), ('existing', 'record')]:
            renamed = re.sub(r'\b' + before + r'\b', after, renamed)
        results = compat.run_syntax([{'model_context': value, 'wake': True} for value in [context, renamed]])['results']
        self.assertEqual(results[0]['model'], results[1]['model'])
        replacement = results[1]['wake_replacement']
        self.assertIn('record&&!plain&&!observable&&(!all||this.m_bIsInitialized)', replacement)
        self.assertNotIn('GetStoreTags()', replacement)
        self.assertNotIn('tags', replacement)
        self.assertIn('record.InitFromProto(item)', replacement)

    def test_new_skipped_side_effect_changes_contract_and_used_accumulator_is_not_removed(self):
        context = CONTEXT[:-1] + WAKE + '}'
        added = context.replace('existing.InitFromProto(item);', 'existing.InitFromProto(item);existing.NewRequiredStep();')
        results = compat.run_syntax([{'model_context': value, 'wake': True} for value in [context, added]])['results']
        self.assertNotEqual(results[0]['model'], results[1]['model'])
        used = context.replace('return changed}', 'return tags.size}')
        self.assertIn('error', compat.run_syntax([{'model_context': used, 'wake': True}])['results'][0])

    def test_factory_headers_guard_hidden_bindings_and_reassignments(self):
        cases = [
            {'factory_header': '(a,b,r)=>{var ns=r(1);class C{get allApps(){return Array.from(this.apps)}}}', 'bindings': ['Array']},
            {'factory_header': '(a,b,r)=>{var ns=r(1);if(flag){var Array=other}class C{get allApps(){return Array.from(this.apps)}}}', 'bindings': ['Array']},
            {'factory_header': '(a,b,r)=>{var ns=r(1);ns=other;}', 'bindings': ['ns']},
        ]
        results = compat.run_syntax(cases)['results']
        self.assertEqual(results[0]['canonical'], '')
        self.assertIn('error', results[1])
        self.assertIn('error', results[2])


if __name__ == '__main__':
    unittest.main()
