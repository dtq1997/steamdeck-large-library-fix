// [Codex] Trusted parser driver. Steam source is parsed as data, never evaluated.
function envelope(text) {
  let suffix = '';
  let body = text;
  if (!/^\d+\s*:/.test(body) && body.endsWith('}),')) {
    suffix = '}),'; body = body.slice(0, -3);
  } else if (body.endsWith(',')) {
    suffix = ','; body = body.slice(0, -1);
  }
  const forms = [[body, 'program']];
  if (/^\d+\s*:/.test(body)) forms.unshift(['({' + body + '})', 'object']);
  forms.push(['class __LLF__{' + body + '}', 'class']);
  for (const [input, kind] of forms) {
    try { acorn.parse(input, {ecmaVersion: 'latest'}); return {input, kind, suffix}; }
    catch (_) { /* Try the other explicitly supported syntax contexts. */ }
  }
  throw Error('Unsupported syntax context');
}

function identifier(index) {
  const alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ$_';
  let result = alphabet[index % alphabet.length];
  for (index = Math.floor(index / alphabet.length); index; index = Math.floor(index / alphabet.length)) {
    result += alphabet[index % alphabet.length];
  }
  return result;
}

function canonical(text) {
  const form = envelope(text);
  const output = Terser.minify_sync(form.input, {
    compress: false, keep_fnames: true, keep_classnames: true,
    mangle: {toplevel: false, nth_identifier: {get: identifier}},
    format: {ascii_only: true, comments: false, semicolons: true},
  }).code;
  const tree = acorn.parse(output, {ecmaVersion: 'latest'});
  if (form.kind === 'class') {
    const body = tree.body[0].body;
    return output.slice(body.start + 1, body.end - 1) + form.suffix;
  }
  if (form.kind === 'object') {
    const object = tree.body[0].expression;
    return output.slice(object.start + 1, object.end - 1) + form.suffix;
  }
  return output + form.suffix;
}

function identifiers(tree) {
  const names = new Set();
  function visit(node) {
    if (!node || typeof node !== 'object') return;
    if (node.type === 'Identifier') names.add(node.name);
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(visit);
      else if (value && typeof value === 'object') visit(value);
    }
  }
  visit(tree);
  return [...names].sort();
}

function scopedHeader(request) {
  const text = request.header_prefix;
  const first = /^var\s+/.exec(text);
  if (!first) throw Error('Import declaration missing');
  const expression = acorn.parseExpressionAt(text, first[0].length, {ecmaVersion: 'latest'});
  const raw = text.slice(0, expression.end) + ';';
  const tree = acorn.parse(raw, {ecmaVersion: 'latest'});
  if (tree.body.length !== 1 || tree.body[0].type !== 'VariableDeclaration') throw Error('Invalid declaration');
  const bindings = new Map(tree.body[0].declarations.map(node => [node.id.name, node]));
  const wanted = new Set(request.bindings);
  let changed = true;
  while (changed) {
    changed = false;
    for (const name of [...wanted]) {
      const node = bindings.get(name);
      if (!node) continue;
      for (const dependency of identifiers(node.init)) {
        if (bindings.has(dependency) && !wanted.has(dependency)) { wanted.add(dependency); changed = true; }
      }
    }
  }
  const selected = tree.body[0].declarations.filter(node => wanted.has(node.id.name));
  const code = selected.length ? 'var ' + selected.map(node => raw.slice(node.start, node.end)).join(',') + ';' : ';';
  return {canonical: canonical(code), raw_header: raw};
}

function codeLocations(source, requested, budgetCandidates, collect) {
  const wanted = new Set(requested), accepted = [], spellings = [];
  const budget = new Map(budgetCandidates.map(item => [item.start, item]));
  const tokens = acorn.tokenizer(source, {ecmaVersion: 'latest'});
  while (true) {
    const token = tokens.getToken();
    if (wanted.has(token.start)) accepted.push(token.start);
    const preset = budget.get(token.start);
    if (preset && token.type.label === 'string' && source.slice(token.start, token.end) === preset.raw &&
        acorn.parseExpressionAt(preset.replacement, 0, {ecmaVersion: 'latest'}).value === token.value) {
      spellings.push({start: token.start, end: token.end, replacement: preset.replacement,
                      saving: byteLength(preset.raw) - byteLength(preset.replacement), value: token.value, raw: preset.raw});
    }
    const raw = collect && token.type.label === 'string' ? source.slice(token.start, token.end) : '';
    if (collect && raw.length <= 1024 && /\\u[0-9a-f{]/i.test(raw) && !token.value.startsWith('use ')) {
      const next = JSON.stringify(token.value).replace(/[\u2028\u2029\u202a-\u202e\u2066-\u2069]/g,
        c => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));
      const saving = byteLength(raw) - byteLength(next);
      if (saving > 0) {
        spellings.push({start: token.start, end: token.end, replacement: next, saving, value: token.value, raw});
      }
    }
    if (token.type.label === 'eof') break;
  }
  spellings.sort((a, b) => b.saving - a.saving || a.start - b.start);
  let total = 0;
  const selected = [];
  for (const item of spellings) {
    if (acorn.parseExpressionAt(item.replacement, 0, {ecmaVersion: 'latest'}).value !== item.value) continue;
    const {value, ...edit} = item;
    selected.push(edit); total += item.saving;
    if (total >= 8192) break;
  }
  return {locations: accepted, spellings: selected};
}

function byteLength(text) {
  let bytes = 0;
  for (const char of text) {
    const point = char.codePointAt(0);
    bytes += point < 128 ? 1 : point < 2048 ? 2 : point < 65536 ? 3 : 4;
  }
  return bytes;
}

const request = JSON.parse(std.in.readAsString());
const results = request.texts.map(text => {
  try {
    if (typeof text === 'object' && text.literal_pair) {
      const values = text.literal_pair.map(value => acorn.parseExpressionAt(value, 0, {ecmaVersion: 'latest'}));
      return {equal: values.every((node, i) => node.type === 'Literal' && typeof node.value === 'string' &&
        node.end === text.literal_pair[i].length) && values[0].value === values[1].value};
    }
    if (typeof text === 'object') return scopedHeader(text);
    const form = envelope(text);
    return {canonical: canonical(text), identifiers: identifiers(acorn.parse(form.input, {ecmaVersion: 'latest'}))};
  }
  catch (error) { return {error: String(error)}; }
});
const scan = request.source ? codeLocations(request.source, request.offsets || [], request.budget_candidates || [], request.collect_spellings || false) : {locations: [], spellings: []};
std.out.puts(JSON.stringify({results, ...scan}) + '\n');
