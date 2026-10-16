"""Lexical dependency certificates for existing optimizations, independent of minified names."""
import re
from itertools import islice

import compat

METHODS = (2, 4, 5, 6, 10, 11, 12, 14)
CONTRACTS = (1, 2, 3, 4, 5, 6, 9, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21)
IDENTIFIER = r'[A-Za-z_$][A-Za-z0-9_$]{0,127}'


def scopes(source):
    matches = list(compat.MODULE.finditer(source))
    result = {}
    for i, match in enumerate(matches):
        key = compat.module_key(match.group())
        if key in result:
            result[key] = None
        else:
            result[key] = (match.start(), matches[i + 1].start() if i + 1 < len(matches) else len(source), match.end() - 1)
    return result


def discover(source, metadata):
    ranges, rows = scopes(source), []
    for entry in metadata.get('structural', []):
        expression = re.compile(r'(?<![\w$])' + compat.SPACE.join(IDENTIFIER if variable else re.escape(token)
                                for token, variable in entry['tokens']))
        scope = ranges.get(entry['scope'])
        matches = list(islice(expression.finditer(source, *(scope[:2] if scope else (0, len(source)))), 65))
        if not matches and scope:
            matches = list(islice(expression.finditer(source), 65))
        if len(matches) > 64:
            continue
        rows.extend({'entry': entry, 'start': match.start(), 'end': match.end(), 'text': match.group()}
                    for match in matches)
    # Public UI protocols locate local edits without depending on their enclosing module ID.
    anchors = [match.start() for match in islice(re.finditer(
        r'CollectionLabelCount|\.collection\.visibleApps|"AllCollectionsGrid"', source), 513)]
    if len(rows) > 256 or len(anchors) > 512:
        raise compat.Incompatible('Too many structural operation candidates')
    return rows, anchors


def contexts(source, native):
    raw = source.encode()
    result = {}
    for unit in native:
        if unit['end'] - unit['start'] > 800000:
            continue
        result[unit['position']] = {**unit, 'context': raw[unit['start']:unit['end']].decode(),
                                   'char_start': len(raw[:unit['start']].decode())}
    return result


def certificate_requests(source, rows, units, offsets, valid):
    requests, selected = [], []
    for row in rows:
        position = offsets[row['start']]
        if position not in valid or position not in units:
            continue
        unit = units[position]
        requests.append({'structural': {'source': row['text'], 'context': unit['context'],
            'start': row['start'] - unit['char_start'], 'outer_bindings': unit['outer_bindings']}})
        selected.append({**row, 'unit': unit})
    return requests, selected


def accepted_certificates(rows, values):
    choices = {}
    for row, value in zip(rows, values):
        entry = row['entry']
        if value.get('canonical') is None or compat.digest(value['canonical']) != entry['digest']:
            continue
        if not set(entry.get('owner', [])).issubset(value.get('owner', [])):
            continue
        actual = {role: name for name, role in value['names']}
        names = [(name, actual[role]) for name, role in entry['names'] if role in actual]
        if len(names) != len(entry['names']) or len(value['named']) != len(entry['named']):
            continue
        key = entry['kind'], entry['index']
        choices.setdefault(key, []).append({**row, 'certificate': value, 'names': names,
                                            'named': list(zip(entry['named'], value['named']))})
    return {key: rows[0] for key, rows in choices.items() if len(rows) == 1}


def adapt_requests(accepted, profile):
    contexts_maps, unsafe = {}, set()
    for row in accepted.values():
        key = row['unit']['start']
        mapping = contexts_maps.setdefault(key, {})
        for old, new in [*row['names'], *row['named']]:
            if old in mapping and mapping[old] != new:
                unsafe.add(key)
            mapping[old] = new
    requests, rows = [], []
    for (kind, index), row in accepted.items():
        key, unit = row['unit']['start'], row['unit']
        if kind != 'methods' or key in unsafe:
            continue
        mapping = contexts_maps[key]
        entry = row['entry']
        globals_ = set(entry['replacement_globals'])
        if any(name in entry['defined'] and name not in mapping for name in globals_):
            continue
        introduced = globals_ - set(entry['defined']) - set(mapping)
        if introduced.intersection([*unit['outer_bindings'], *row['certificate']['defined']]):
            continue
        requests.append({'adapt_patch': {'source': profile['methods'][index]['replacement'],
            'names': list(mapping.items()), 'named': row['named'], 'context': unit['context']}})
        rows.append((index, row))
    return requests, rows


def certify(source, profile, metadata, rows, native, offsets, valid, found):
    units = contexts(source, native)
    requests, selected = certificate_requests(source, rows, units, offsets, valid)
    accepted = accepted_certificates(selected, compat.run_syntax(requests)['results']) if requests else {}
    for (kind, index), row in accepted.items():
        if kind in ('contracts', 'interfaces'):
            found.setdefault(kind, {}).setdefault(index, row)
    requests, selected = adapt_requests(accepted, profile)
    for (index, row), value in zip(selected, compat.run_syntax(requests)['results'] if requests else []):
        if 'replacement' in value:
            found['methods'].setdefault(index, {**row, 'replacement': value['replacement'], 'kind': 'original'})
    unique = {unit['start']: unit for unit in units.values()}
    return list(unique.values())


def local_operations(units):
    units = [unit for unit in units if any(marker in unit['context'] for marker in
             ('CollectionLabelCount', '"AllCollectionsGrid"', 'GetChildren('))]
    requests = [{'operation_edits': {'context': unit['context'], 'outer_bindings': unit['outer_bindings']}}
                for unit in units]
    groups = {}
    results = compat.run_syntax(requests)['results'] if requests else []
    for unit, result in zip(units, results):
        for item in result.get('operations', []):
            edits = [(unit['char_start'] + a, unit['char_start'] + b, text)
                     for a, b, text in compat.spelling_edits(unit['context'], item['edits'])]
            groups.setdefault(item['feature'], []).append(edits)
    return {key: values[0] for key, values in groups.items() if len(values) == 1}


def overlaps(changes, groups, name):
    occupied = [edit for key, items in groups.items() if not (key == 'collections' and name == 'library')
                for edit in items]
    return any(a < end and start < b for a, b, _ in changes for start, end, _ in occupied)


def needs_projection(found, metadata):
    return any(not all(index in found[kind] for index in group[kind])
               for group in metadata['groups'] if group['id'] in ('library', 'navigation')
               for kind in ('methods', 'contracts'))


def extend_groups(found, metadata, active, paused, groups):
    operations = found.get('operations', {})
    requirements = {'library': ([2, 4, 5, 6, 10, 12, 14],
                    [i for i in CONTRACTS if i != 13], ['count', 'preview']),
                    'navigation': ([11], [13], ['theme', 'scroll'])}
    for name, (methods, contracts, needed) in requirements.items():
        if name in active or not all(index in found['methods'] for index in methods):
            continue
        if not all(index in found['contracts'] for index in contracts) or not all(key in operations for key in needed):
            continue
        if name == 'navigation' and not all(index in found.get('interfaces', {}) for index in (0, 1)):
            continue
        changes = [(found['methods'][i]['start'], found['methods'][i]['end'],
                    found['methods'][i].get('replacement', metadata['methods'][i]['replacement']))
                   for i in methods]
        changes.extend(edit for key in needed for edit in operations[key])
        if overlaps(changes, groups, name):
            continue
        if name == 'library' and 'collections' in active:
            active.remove('collections')
            groups.pop('collections', None)
        active.append(name)
        paused.pop(name, None)
        groups[name] = changes


def generate(source, profile, metadata):
    """Compile reusable certificates from an existing baseline, never the latest client."""
    import generate_compat
    ranges, rows, indexes = scopes(source), [], []
    for kind, ids in (('methods', METHODS), ('contracts', CONTRACTS)):
        for index in ids:
            entry = metadata[kind][index]
            if kind == 'methods':
                start, end = metadata['ranges']['original'][index]
                text = source[start:end]
            else:
                hint = metadata['methods'][14]['scope'] if index == 6 else None
                start, end, text = generate_compat.exact_fragment(source, profile[kind][index], scope=hint)
            scope = compat.module_key(entry['scope'])
            indexes.append((kind, index, scope))
            rows.append({'start': start, 'end': end, 'text': text, 'position': ranges[scope][2]})
    grid_position = ranges[compat.module_key(metadata['methods'][7]['scope'])][2]
    positions = compat.byte_offsets(source, [grid_position, *[row['position'] for row in rows]])
    native = compat.run_native(source, [], [], factories=sorted(set(positions.values())))
    units = contexts(source, native['factories'])
    grid = units[positions[grid_position]]
    interfaces = compat.run_syntax([{'operation_interfaces': grid['context']}])['results'][0]
    for index, (a, b, text) in enumerate(compat.spelling_edits(grid['context'],
            [{**item, 'replacement': ''} for item in interfaces['interfaces']])):
        indexes.append(('interfaces', index, compat.module_key(metadata['methods'][7]['scope'])))
        rows.append({'start': grid['char_start'] + a, 'end': grid['char_start'] + b,
                     'text': grid['context'][a:b], 'position': ranges[compat.module_key(metadata['methods'][7]['scope'])][2]})
    requests = [{'structural': {'source': row['text'], 'context': units[positions[row['position']]]['context'],
                'start': row['start'] - units[positions[row['position']]]['char_start']}} for row in rows]
    certificates = compat.run_syntax(requests)['results']
    tokens = compat.run_syntax([{'structural_tokens': row['text']} for row in rows])['results']
    globals_ = compat.run_syntax([profile['methods'][index]['replacement'] if kind == 'methods' else '{}'
                                 for kind, index, _ in indexes])['results']
    result = []
    for (kind, index, scope), certificate, token, global_ in zip(indexes, certificates, tokens, globals_):
        if 'canonical' not in certificate or 'tokens' not in token or 'identifiers' not in global_:
            raise compat.Incompatible(str((kind, index, certificate, token, global_)))
        result.append({'kind': kind, 'index': index, 'scope': scope, 'tokens': token['tokens'],
                       'digest': compat.digest(certificate['canonical']), 'names': certificate['names'],
                       'named': certificate['named'], 'defined': certificate['defined'],
                       'owner': certificate['owner'],
                       'replacement_globals': global_['identifiers']})
    return result
