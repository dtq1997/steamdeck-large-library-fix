"""[Codex] Generate semantic fingerprints from locally supplied original clients."""
import argparse
import json
import re
from pathlib import Path

import compat
import engine

MODULE = re.compile(r'(?<![\w$])\d+(?:e\d+)?:(?:\(?\([\w$,]+\)|\(?[\w$]+)=>\{')
HEADERS = {}


def group_definitions(modern):
    if not modern:
        return [{'id': 'wake', 'methods': [1], 'contracts': []},
                {'id': 'startup', 'methods': [2], 'contracts': []},
                {'id': 'library', 'methods': [0], 'contracts': [0, 1]}]
    nav_contracts = [0, 7, 8, 10, 13]
    return [{'id': 'wake', 'methods': [0], 'contracts': []},
            {'id': 'startup', 'methods': [1], 'contracts': []},
            {'id': 'library', 'methods': [2, 3, 4, 5, 6, 9, 10, 12, 14],
             'contracts': [i for i in range(22) if i not in nav_contracts]},
            {'id': 'navigation', 'methods': [7, 8, 11, 13], 'contracts': nav_contracts, 'css': True}]


def exact_fragment(source, rule, kind='original', scope=None):
    expected = rule.get(kind + '_sha256', rule.get('sha256'))
    if 'literal' in rule:
        matches = list(re.finditer(re.escape(rule['literal']), source))
        if scope is not None:
            matches = [m for m in matches if enclosing_module(source, m.start()).group() == scope]
        if len(matches) != 1:
            raise ValueError('Ambiguous literal: ' + rule['literal'][:60])
        match = matches[0]
        return match.start(), match.end(), match.group()
    accepted = [(a, b, text) for a, b, text in engine.fragments(source, rule)
                if compat.digest(text) == expected]
    if len(accepted) != 1:
        raise ValueError('Unknown original fragment: ' + rule['start'])
    return accepted[0]


def enclosing_module(source, start):
    matches = [m for m in MODULE.finditer(source, 0, start + 256) if m.start() <= start]
    if not matches:
        raise ValueError('No enclosing webpack module')
    return matches[-1]


def scope_header(source, start, end, bindings):
    scope = enclosing_module(source, start).group()
    key = (hash(source), scope, tuple(bindings))
    if key in HEADERS:
        return HEADERS[key]
    found = compat.header(source, scope, start, end)
    if not found:
        raise ValueError('No enclosing import declaration')
    header_start = found[1]
    if header_start is None:
        if start != found[0]:
            raise ValueError('Missing import guard for a nested fragment')
        return scope, None, ''
    checked = compat.run_syntax([{'header_prefix': source[header_start:header_start + 16000],
                                  'bindings': bindings}])['results'][0]
    if 'canonical' not in checked:
        raise ValueError('Could not parse import declaration: ' + str(checked))
    HEADERS[key] = (scope, compat.digest(checked['canonical']), checked['raw_header'])
    return HEADERS[key]


def profile_metadata(source, profile, css):
    patched = engine.build(source.encode(), profile).decode()
    result = {'methods': [], 'contracts': [], 'groups': group_definitions('target' in profile),
              'known': {'original': compat.digest(source), 'patched': compat.digest(patched)},
              'ranges': {'original': [], 'patched': []}}
    result['groups'][0]['contracts'] = profile.get('wake_contracts', [])
    if css is not None:
        result['css_digest'] = compat.css_digest(css)
    for key in ('methods', 'contracts'):
        for index, rule in enumerate(profile[key]):
            hint = result['methods'][14]['scope'] if key == 'contracts' and index == 6 and 'target' in profile else None
            start, end, text = exact_fragment(source, rule, scope=hint)
            items = [text, rule['replacement']] if key == 'methods' else [text]
            checked = compat.run_syntax(items)['results']
            if any('error' in item for item in checked):
                raise ValueError((rule, checked))
            bindings = sorted(set(name for item in checked for name in item['identifiers']))
            scope, header, text_header = scope_header(source, start, end, bindings)
            entry = {'original': compat.digest(checked[0]['canonical']), 'scope': scope,
                     'header': header, 'header_text': text_header, 'bindings': bindings}
            if key == 'methods':
                entry.update(patched=compat.digest(checked[1]['canonical']), replacement=checked[1]['canonical'])
                result['ranges']['original'].append([start, end])
                a, b, _ = exact_fragment(patched, rule, 'patched')
                result['ranges']['patched'].append([a, b])
            result[key].append(entry)
    values = compat.run_syntax([], source, collect=True)['spellings']
    edits = compat.spelling_edits(source, values)
    result['spelling_budget'] = [{'start': start, 'end': end, 'replacement': replacement, 'raw': source[start:end]}
                               for start, end, replacement in edits if source.count(source[start:end]) == 1]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--old-bundle', required=True, type=Path)
    parser.add_argument('--new-bundle', required=True, type=Path)
    parser.add_argument('--css', required=True, type=Path)
    args = parser.parse_args()
    rules = json.loads((engine.HERE / 'rules.json').read_text())
    result = {'version': engine.VERSION, 'profiles': {}}
    for profile, file, css in [(rules, args.old_bundle, None),
                               (rules['alternates'][0], args.new_bundle, args.css.read_bytes())]:
        result['profiles'][profile['tested_client']] = profile_metadata(file.read_bytes().decode('utf-8'), profile, css)
        print('Generated profile', profile['tested_client'])
    (engine.HERE / 'compatibility.json').write_text(json.dumps(result, ensure_ascii=True, indent=2) + '\n')


if __name__ == '__main__':
    main()
