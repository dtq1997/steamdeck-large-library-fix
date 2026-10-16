// [Codex] Closed lexical certificates and capture-avoiding patch adaptation.
function fragmentRoots(form) {
  const tree = acorn.parse(form.input, {ecmaVersion: 'latest'}), roots = new Set();
  for (const statement of tree.body) {
    if (['FunctionDeclaration', 'ClassDeclaration'].includes(statement.type)) roots.add(statement.id.name);
    if (statement.type === 'VariableDeclaration') for (const declaration of statement.declarations) {
      if (declaration.id.type === 'Identifier') roots.add(declaration.id.name);
    }
  }
  return roots;
}

const structuralGlobalsCache = new Map();
let structuralGlobalsSize = 0;
function structuralGlobals(code) {
  if (structuralGlobalsCache.has(code)) return structuralGlobalsCache.get(code);
  const names = modelGlobals(code).map(([name]) => name);
  if (structuralGlobalsCache.size >= 128 || structuralGlobalsSize + code.length > 2000000) {
    structuralGlobalsCache.clear(); structuralGlobalsSize = 0;
  }
  structuralGlobalsCache.set(code, names);
  structuralGlobalsSize += code.length;
  return names;
}

function projectedDependencies(input, factory, form, start) {
  const definitions = modelDefinitions(factory), selected = new Map(), roots = fragmentRoots(form);
  const pending = structuralGlobals(form.input).slice();
  let budget = 0;
  while (pending.length) {
    const name = pending.shift(), node = definitions.get(name);
    if (!node || selected.has(name) || roots.has(name)) continue;
    if (start >= 0 && node.start <= start + 1 && start + 1 < node.end) { roots.add(name); continue; }
    selected.set(name, node);
    const code = (node.type === 'VariableDeclarator' ? 'var ' : '') + input.slice(node.start, node.end) + ';';
    budget += code.length;
    if (budget > 600000 || selected.size > 256) throw Error('Dependency certificate exceeds limit');
    for (const dependency of structuralGlobals(code)) if (definitions.has(dependency)) pending.push(dependency);
  }
  return {definitions, selected, roots};
}

function projectionWrites(input, factory, state, start, length, bindings) {
  const names = new Set([...state.selected.keys(), ...state.roots]), writes = [];
  const references = new Set([...names].flatMap(name => bindings.get(name) || []));
  for (const statement of factory.body.body) {
    localWalk(statement, node => {
      const target = ['AssignmentExpression', 'ForInStatement', 'ForOfStatement'].includes(node.type) ? node.left : node.type === 'UpdateExpression' ? node.argument : null;
      if (!target) return;
      let relevant = false;
      localWalk(target, part => { if (part.type === 'Identifier' && references.has(part.start)) relevant = true; });
      if (!relevant) return;
      if (start >= 0 && start + 1 <= node.start && node.end <= start + 1 + length) return;
      if ([...state.selected.values()].some(declaration => declaration.start <= node.start && node.end <= declaration.end)) return;
      // Top-level rebinding is checked as code; hidden mutable closures are rejected.
      if (target.type !== 'Identifier' || statement.type !== 'ExpressionStatement' || statement.expression !== node) throw Error('Mutable dependency outside certificate');
      writes.push(input.slice(statement.start, statement.end));
    });
  }
  if (localBindings(factory, new Set())) throw Error('Dynamic dependency scope');
  return writes;
}

const structuralScopes = new Map();
function structuralScope(context) {
  if (structuralScopes.has(context)) return structuralScopes.get(context);
  const input = '(' + context + ')', factory = acorn.parse(input, {ecmaVersion: 'latest'}).body[0].expression;
  const tree = Terser.minify_sync(input, {compress: false, mangle: false, format: {ast: true, code: false}}).ast;
  tree.figure_out_scope();
  const bindings = new Map([...tree.body[0].body.variables].map(([name, definition]) =>
    [name, definition.references.map(reference => reference.start.pos)]));
  const value = {input, factory, bindings};
  if (structuralScopes.size >= 16) structuralScopes.clear();
  structuralScopes.set(context, value);
  return value;
}

function namedDefinitions(source) {
  const form = envelope(source), tree = acorn.parse(form.input, {ecmaVersion: 'latest'}), names = [];
  localWalk(tree, node => {
    if (['FunctionDeclaration', 'FunctionExpression', 'ClassDeclaration', 'ClassExpression'].includes(node.type) &&
        node.id?.name && node.id.name !== '__LLF__') names.push(node.id.name);
  });
  return names;
}

function structuralOwner(factory, position, input) {
  let owner = null;
  localWalk(factory, node => {
    if (['ClassDeclaration', 'ClassExpression'].includes(node.type) && node.start <= position && position < node.end) owner = node;
  });
  if (!owner) return {members: [], header: ''};
  if (owner.body.body.some(member => member.computed || member.type === 'StaticBlock')) throw Error('Dynamic operation owner');
  return {members: owner.body.body.map(member => member.key.name || member.key.value),
    header: owner.superClass ? ';(' + input.slice(owner.superClass.start, owner.superClass.end) + ');' : ''};
}

function structuralCertificate(request) {
  if (request.context.length > 800000 || request.source.length > 100000) throw Error('Structural scope exceeds limit');
  const form = envelope(request.source), {input, factory, bindings} = structuralScope(request.context);
  if (!factory.body?.body || factory.params.some(parameter => parameter.type !== 'Identifier')) throw Error('Unsupported factory');
  const owner = structuralOwner(factory, request.start + 1, input);
  const projected = {...form, input: form.input + owner.header};
  const state = projectedDependencies(input, factory, projected, request.start);
  const declarations = [...state.selected.values()].sort((a, b) => a.start - b.start).map(node =>
    (node.type === 'VariableDeclarator' ? 'var ' : '') + input.slice(node.start, node.end) + ';');
  const declared = fragmentRoots(form), placeholders = [...state.roots].filter(name => !declared.has(name));
  const code = '(' + input.slice(factory.start, factory.body.start + 1) + declarations.join('\n') +
    (placeholders.length ? 'var ' + placeholders.join(',') + ';' : '') + projected.input +
    '\n' + projectionWrites(input, factory, state, request.start, request.source.length, bindings).join('\n') + '})';
  const result = Terser.minify_sync(code, {compress: false,
    mangle: {toplevel: true, nth_identifier: {get: identifier}}, format: {ast: true, ascii_only: true}});
  const arrow = result.ast.body[0].body;
  const globals = modelGlobals(code).map(([name]) => name);
  if (globals.some(name => (request.outer_bindings || []).includes(name))) throw Error('Shadowed external dependency');
  return {canonical: result.code, names: [...arrow.variables].map(([name, definition]) =>
    [name, definition.mangled_name || name]), named: namedDefinitions(request.source),
    defined: [...state.definitions.keys()], owner: owner.members};
}

function structuralTokens(source) {
  const form = envelope(source), tree = acorn.parse(form.input, {ecmaVersion: 'latest'});
  const names = new Set(), keys = new Set();
  const shift = form.kind === 'class' ? 'class __LLF__{'.length : form.kind === 'object' ? 2 : 0;
  localWalk(tree, (node, parent) => {
    if (node.type === 'Identifier') names.add(node.start - shift);
    if ((parent?.type === 'MemberExpression' && parent.property === node && !parent.computed) ||
        (['Property', 'MethodDefinition', 'PropertyDefinition'].includes(parent?.type) && parent.key === node && !parent.computed)) keys.add(node.start - shift);
  });
  const lexer = acorn.tokenizer(source, {ecmaVersion: 'latest'}), tokens = [];
  while (true) {
    const token = lexer.getToken();
    if (token.type.label === 'eof') break;
    tokens.push([source.slice(token.start, token.end), names.has(token.start) && !keys.has(token.start)]);
  }
  return tokens;
}

function adaptedPatch(request) {
  const form = envelope(request.source), mapping = new Map(request.names);
  const result = Terser.minify_sync(form.input, {compress: false, mangle: false, format: {ast: true, code: false}});
  const tree = result.ast;
  tree.figure_out_scope();
  const targets = new Map([...tree.globals, ...tree.variables]);
  for (const [name, actual] of mapping) if (targets.has(name)) targets.get(name).mangled_name = actual;
  const forbidden = new Set([...mapping.values(), ...mapping.keys()]);
  localWalk(acorn.parse('(' + request.context + ')', {ecmaVersion: 'latest'}), node => {
    if (node.type === 'Identifier') forbidden.add(node.name);
  });
  let index = 0;
  const named = new Map(request.named || []);
  tree.walk({_visit(node, descend) {
    if (node.variables instanceof Map) for (const [name, definition] of node.variables) {
      if (definition.mangled_name) continue;
      if (named.has(name) && definition.orig.some(symbol => /Symbol(?:Defun|Lambda|Class)/.test(symbol.TYPE))) {
        definition.mangled_name = named.get(name);
      } else {
        let fresh;
        do { fresh = '__llf' + index++; } while (forbidden.has(fresh));
        definition.mangled_name = fresh;
      }
    }
    if (descend) descend.call(node);
  }});
  const printed = tree.print_to_string();
  const output = Terser.minify_sync(printed, {compress: false, keep_fnames: true, keep_classnames: true,
    mangle: {toplevel: false, nth_identifier: {get: identifier}}, format: {ascii_only: true}}).code;
  const parsed = acorn.parse(output, {ecmaVersion: 'latest'});
  if (form.kind === 'class') {
    const body = parsed.body[0].body;
    return {replacement: output.slice(body.start + 1, body.end - 1) + form.suffix};
  }
  if (form.kind === 'object') {
    const object = parsed.body[0].expression;
    return {replacement: output.slice(object.start + 1, object.end - 1) + form.suffix};
  }
  return {replacement: output + form.suffix};
}
