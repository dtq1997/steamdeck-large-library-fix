"""[Codex] Bounded syntax checks and feature plans; no Steam code execution."""
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from bisect import bisect_right
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN = re.compile(r'[A-Za-z_$][\w$]*|\d+|"(?:[^"\\]|\\.)*"|\S')
SPACE = r'(?:\s|/\*[\s\S]*?\*/)*'
MODULE = re.compile(r'(?<![\w$])\d+(?:e\d+)?\s*:\s*(?:\(?\s*\([\w$,\s]+\)|\(?\s*[\w$]+)\s*=>\s*\{')


class Incompatible(ValueError):
    pass


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def pattern(text):
    return re.compile(r'(?<![\w$])' + SPACE.join(re.escape(x.group()) for x in TOKEN.finditer(text)))


def candidates(source, rule, begin=0, limit=None):
    ending = pattern(rule['end'])
    for opening in pattern(rule['start']).finditer(source, begin, len(source) if limit is None else limit):
        end = ending.search(source, opening.end(), opening.start() + 24000)
        if end:
            yield opening.start(), end.start(), source[opening.start():end.start()]


def run_syntax(texts, source='', offsets=(), budget_candidates=(), collect=False):
    runner = HERE / 'qjs-linux-x86_64'
    if platform.system() == 'Darwin':
        runner = Path(os.environ.get('LLF_DEVELOPMENT_QJS', str(runner)))
    command = ['/usr/bin/nice', '-n', '10', str(runner), '--memory-limit', str(384 * 1024), '--stack-size', '2048',
               '--std', '-I', str(HERE / 'acorn.js'), '-I', str(HERE / 'terser.js'), str(HERE / 'syntax.js')]
    request = json.dumps({'texts': texts, 'source': source, 'offsets': list(offsets),
                          'budget_candidates': list(budget_candidates), 'collect_spellings': collect}, ensure_ascii=True)
    try:
        result = subprocess.run(command, input=request, text=True, encoding='utf-8', capture_output=True,
                                timeout=20, env={'LANG': 'C.UTF-8'})
    except (OSError, subprocess.SubprocessError) as error:
        raise Incompatible('Syntax checker unavailable: ' + str(error)) from error
    if result.returncode:
        raise Incompatible('Syntax checker refused input: ' + result.stderr[:500])
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise Incompatible('Invalid syntax checker response') from error
    if len(value['results']) != len(texts):
        raise Incompatible('Incomplete syntax checker response')
    return value


def utf16_offsets(source, offsets):
    output, previous, units = {}, 0, 0
    for offset in sorted(set(offsets)):
        units += len(source[previous:offset].encode('utf-16-le')) // 2
        output[offset] = units
        previous = offset
    return output


def spelling_edits(source, values):
    astral = [match.start() + index for index, match in enumerate(re.finditer(r'[\U00010000-\U0010ffff]', source))]
    output = []
    for item in values:
        start = item['start'] - bisect_right(astral, item['start'] - 1)
        end = item['end'] - bisect_right(astral, item['end'] - 1)
        output.append((start, end, item['replacement']))
    return output


def budget_candidates(source, metadata):
    found, saving = [], 0
    for item in metadata.get('spelling_budget', []):
        start = item['start']
        if not source.startswith(item['raw'], start):
            start = source.find(item['raw'], max(0, start - 4096), start + len(item['raw']) + 4096)
        if start < 0:
            start = source.find(item['raw'])
        if start >= 0:
            found.append({**item, 'start': start})
            saving += len(item['raw'].encode()) - len(item['replacement'].encode())
            if saving >= 4096:
                break
    return found


def byte_offsets(source, offsets):
    output, previous, units = {}, 0, 0
    for offset in sorted(set(offsets)):
        units += len(source[previous:offset].encode('utf-8'))
        output[offset] = units
        previous = offset
    return output


def run_native(source, offsets, budgets):
    path = HERE / 'libsyntax-linux-x86_64.so'
    if platform.system() == 'Darwin':
        path = Path(os.environ.get('LLF_DEVELOPMENT_LIBRARY', str(path)))
    command = [sys.executable, str(HERE / 'native_syntax.py'), str(path)]
    request = json.dumps({'source': source, 'offsets': list(offsets), 'budget_candidates': budgets})
    try:
        call = subprocess.run(command, input=request, text=True, encoding='utf-8', capture_output=True,
                              timeout=20, env={'LANG': 'C.UTF-8'})
        result = json.loads(call.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise Incompatible('Native syntax checker unavailable: ' + str(error)) from error
    if call.returncode or 'error' in result:
        raise Incompatible('Native syntax checker refused input: ' + str(result.get('error', call.returncode)))
    return result


def header(source, scope, before, end=None):
    index = source.rfind(scope, 0, before + len(scope) + 1)
    if index >= 0:
        opening_start, opening_end = index, index + len(scope)
    else:
        matches = list(pattern(scope).finditer(source, 0, before + len(scope) + 100))
        if not matches:
            return None
        opening_start, opening_end = matches[-1].start(), matches[-1].end()
    limit = min(opening_end + 16000, end if end is not None else before)
    found = re.search(r'\bvar\s+', source[opening_end:limit])
    if not found:
        return opening_start, None
    start = opening_end + found.start()
    return opening_start, start


def css_digest(data):
    # Only ignore EOF comments/whitespace, never rewrite CSS tokens or selectors.
    text = data.decode('utf-8').rstrip()
    while text.endswith('*/'):
        start = text.rfind('/*')
        if start < 0:
            break
        text = text[:start].rstrip()
    return digest(text)


def module_ranges(source):
    module_matches = list(MODULE.finditer(source))
    modules = [match.start() for match in module_matches]
    ranges = {}
    for index, match in enumerate(module_matches):
        key = re.sub(r'\s+', '', match.group())
        ranges.setdefault(key, []).append((match.start(), modules[index + 1] if index + 1 < len(modules) else len(source)))
    return modules, ranges


def scoped_matches(source, rule, expected, ranges):
    matches = []
    for begin, limit in ranges.get(expected['scope'], []):
        if 'literal' in rule:
            matches.extend((m.start(), m.end(), m.group()) for m in pattern(rule['literal']).finditer(source, begin, limit))
        else:
            matches.extend(candidates(source, rule, begin, limit))
    return matches


def raw_kind(text, rule, key):
    for kind in ('original', 'patched') if key == 'methods' else ('original',):
        raw_hash = rule.get(kind + '_sha256', rule.get('sha256'))
        if raw_hash == digest(text) or ('literal' in rule and rule['literal'] == text):
            return kind
    return None


def prepare_rows(source, rule, expected, key, modules, ranges, add):
    rows = []
    for start, end, text in scoped_matches(source, rule, expected, ranges):
        scope = header(source, expected['scope'], start, end)
        if scope is None:
            continue
        scope_start, header_start = scope
        owner = bisect_right(modules, start) - 1
        if owner < 0 or modules[owner] != scope_start:
            continue
        headers = []
        verified = expected['header'] is None
        if header_start is not None and source.startswith(expected['header_text'], header_start):
            verified = True
        elif header_start is not None:
            headers.append(add({'header_prefix': source[header_start:header_start + 16000],
                                'bindings': expected['bindings']}))
        elif expected['header'] is not None:
            continue
        direct = raw_kind(text, rule, key)
        rows.append({'start': start, 'end': end, 'text': text, 'request': None if direct else add(text),
                     'direct': direct, 'headers': headers, 'header_verified': verified, 'scope_start': scope_start})
    return rows


def accepted_rows(rows, expected, key, checked, offsets, valid):
    accepted = []
    for row in rows:
        value = checked['results'][row['request']].get('canonical') if row['request'] is not None else None
        if (value is None and not row['direct']) or offsets[row['start']] not in valid:
            continue
        if offsets[row['scope_start']] not in valid:
            continue
        headers = [checked['results'][i].get('canonical') for i in row['headers']]
        if not row['header_verified'] and not any(value is not None and digest(value) == expected['header'] for value in headers):
            continue
        for kind in ('original', 'patched') if key == 'methods' else ('original',):
            if row['direct'] == kind or (value is not None and digest(value) == expected[kind]):
                accepted.append({**row, 'kind': kind})
    return accepted


def prepare(source, profile, metadata):
    texts, lookup, requests, positions = [], {}, [], []
    modules, ranges = module_ranges(source)
    def add(text):
        key = json.dumps(text, sort_keys=True) if isinstance(text, dict) else text
        if key not in lookup:
            lookup[key] = len(texts)
            texts.append(text)
        return lookup[key]
    for key in ('methods', 'contracts'):
        for index, rule in enumerate(profile[key]):
            expected = metadata[key][index]
            rows = prepare_rows(source, rule, expected, key, modules, ranges, add)
            positions.extend(position for row in rows for position in (row['start'], row['scope_start']))
            requests.append((key, index, expected, rows))
    pool = budget_candidates(source, metadata)
    positions.extend(position for item in pool for position in (item['start'], item['start'] + len(item['raw'])))
    offsets = byte_offsets(source, positions)
    budget = [{**item, 'start': offsets[item['start']], 'end': offsets[item['start'] + len(item['raw'])]} for item in pool]
    native = run_native(source, offsets.values(), budget)
    budget_requests = [(index, add({'literal_pair': [pool[index]['raw'], pool[index]['replacement']]}))
                       for index in native['budgets']]
    checked = run_syntax(texts)
    valid = set(native['locations'])
    found = {'methods': {}, 'contracts': {}}
    for key, index, expected, rows in requests:
        accepted = accepted_rows(rows, expected, key, checked, offsets, valid)
        if len(accepted) == 1:
            found[key][index] = accepted[0]
    found['spellings'] = [(pool[index]['start'], pool[index]['start'] + len(pool[index]['raw']), pool[index]['replacement'])
                          for index, request in budget_requests if checked['results'][request].get('equal')]
    return found


def known(source, profile, metadata):
    checksum = digest(source)
    kind = next((k for k, value in metadata['known'].items() if checksum == value), None)
    if kind is None:
        return None
    found = {'methods': {}, 'contracts': {i: True for i in range(len(profile['contracts']))},
             'spellings': [(item['start'], item['end'], item['replacement']) for item in metadata.get('spelling_budget', [])] if kind == 'original' else []}
    for index, rule in enumerate(profile['methods']):
        start, end = metadata['ranges'][kind][index]
        found['methods'][index] = {'start': start, 'end': end, 'kind': kind}
    return found


def fit_budget(source, active, paused, groups, spellings):
    def delta(changes):
        return sum(len(text.encode()) - len(source[start:end].encode()) for start, end, text in changes)
    def available(changes):
        return sum(len(source[a:b].encode()) - len(text.encode()) for a, b, text in spellings
                   if not any(a < end and start < b for start, end, _ in changes))
    for name in ['startup', 'library', 'navigation', 'wake']:
        changes = [change for group in active for change in groups[group]]
        if delta(changes) <= available(changes):
            return changes
        if name in active and delta(groups[name]) > 0:
            active.remove(name)
            paused[name] = 'Not enough independently compatible file space'
    return [change for group in active for change in groups[group]]


def plan(source, profile, metadata, css):
    exact = known(source, profile, metadata)
    found = exact if exact is not None else prepare(source, profile, metadata)
    active, paused, groups, kinds = [], {}, {}, set()
    for group in metadata['groups']:
        ready = all(i in found['methods'] for i in group['methods'])
        ready = ready and all(i in found['contracts'] for i in group['contracts'])
        if group.get('css'):
            ready = ready and css is not None and css_digest(css) == metadata['css_digest']
        if not ready:
            if any(found['methods'].get(i, {}).get('kind') == 'patched' for i in group['methods']):
                raise Incompatible('Changed dependencies of an unmanaged existing patch')
            paused[group['id']] = 'Related code or layout is not recognized'
            continue
        active.append(group['id'])
        changes = groups.setdefault(group['id'], [])
        for index in group['methods']:
            item = found['methods'][index]
            kinds.add(item['kind'])
            if item['kind'] == 'original':
                replacement = profile['methods'][index]['replacement'] if exact is not None else metadata['methods'][index]['replacement']
                changes.append((item['start'], item['end'], replacement))
    changes = fit_budget(source, active, paused, groups, found.get('spellings', []))
    if not active:
        raise Incompatible('No compatible optimization group')
    return {'active': active, 'paused': paused, 'changes': changes, 'syntax_checked': exact is None,
            'spellings': found.get('spellings', []),
            'kind': 'patched' if kinds == {'patched'} else 'original' if kinds == {'original'} else 'mixed'}
