// [Codex] Fingerprint the model operations used by wake, rather than unrelated UI helpers.
function modelGlobals(source) {
  const result = Terser.minify_sync(source, {compress: false, mangle: false, format: {ast: true, code: false}});
  result.ast.figure_out_scope();
  return [...result.ast.globals].sort((a, b) =>
    a[1].references[0].start.pos - b[1].references[0].start.pos);
}

function modelMembers(node, roots, allFields) {
  if (node.body.body.some(member => member.computed || member.type === 'StaticBlock')) throw Error('Dynamic model definition');
  const wanted = new Set(roots);
  if (allFields) node.body.body.filter(member => member.type === 'PropertyDefinition').forEach(member => wanted.add(member.key.name));
  let previous = -1;
  while (previous !== wanted.size) {
    previous = wanted.size;
    for (const member of node.body.body.filter(member => wanted.has(member.key?.name))) {
      localWalk(member, part => {
        if (part.type === 'MemberExpression' && !part.computed && part.object.type === 'ThisExpression') wanted.add(part.property.name);
      });
    }
  }
  return node.body.body.filter(member => wanted.has(member.key?.name));
}

function modelDefinitions(factory) {
  const definitions = new Map();
  const add = (name, node) => {
    if (definitions.has(name)) throw Error('Ambiguous model binding');
    definitions.set(name, node);
  };
  for (const statement of factory.body.body) {
    if (statement.type === 'VariableDeclaration') {
      for (const item of statement.declarations) {
        if (item.id.type === 'Identifier') add(item.id.name, item);
      }
    } else if (['FunctionDeclaration', 'ClassDeclaration'].includes(statement.type)) add(statement.id.name, statement);
  }
  return definitions;
}

function scopedFactoryHeader(request) {
  if (request.factory_header.length > 800000) throw Error('Header scope exceeds limit');
  const input = '(' + request.factory_header + ')';
  const factory = acorn.parse(input, {ecmaVersion: 'latest'}).body[0].expression;
  const definitions = modelDefinitions(factory), wanted = new Set(request.bindings);
  let previous = -1;
  while (previous !== wanted.size) {
    previous = wanted.size;
    for (const name of [...wanted]) {
      const node = definitions.get(name);
      if (!node) {
        if (localBindings(factory, new Set([name]))) throw Error('Shadowed or hoisted dependency');
        continue;
      }
      if (node.type !== 'VariableDeclarator') throw Error('Unsupported header binding');
      const code = node.init ? '(' + input.slice(node.init.start, node.init.end) + ')' : 'undefined';
      for (const dependency of modelGlobals(code).map(([name]) => name)) {
        if (definitions.has(dependency)) wanted.add(dependency);
      }
    }
  }
  const selected = [...definitions].filter(([name]) => wanted.has(name)).map(([, node]) => node).sort((a, b) => a.start - b.start);
  modelScopeGuard(input, factory, [...wanted].filter(name => definitions.has(name)), null, []);
  const text = selected.length ? 'var ' + selected.map(node => input.slice(node.start, node.end)).join(',') + ';' : ';';
  return {canonical: Terser.minify_sync(text, {compress: false, mangle: false, format: {ascii_only: true}}).code};
}

function modelScopeGuard(input, factory, names, schemaName, ranges) {
  const tree = Terser.minify_sync(input, {compress: false, mangle: false, format: {ast: true, code: false}}).ast;
  tree.figure_out_scope();
  const bindings = tree.body[0].body.variables, writes = new Set();
  localWalk(factory, node => {
    const target = ['AssignmentExpression', 'ForInStatement', 'ForOfStatement'].includes(node.type) ? node.left : node.type === 'UpdateExpression' ? node.argument : null;
    if (target && target.type !== 'VariableDeclaration') localWalk(target, part => {if (part.type === 'Identifier') writes.add(part.start);});
    if (node.type === 'WithStatement' || (node.type === 'CallExpression' && node.callee.name === 'eval')) throw Error('Dynamic model scope');
  });
  for (const name of names) {
    const binding = bindings.get(name);
    if (!binding || binding.orig.length !== 1 || binding.references.some(reference => writes.has(reference.start.pos))) throw Error('Mutable model dependency');
    if (name === schemaName && binding.references.some(reference =>
      !ranges.some(node => node.start <= reference.start.pos && reference.start.pos < node.end))) throw Error('Model schema escapes verified operations');
  }
}

function modelReflection(code) {
  const program = acorn.parse(code, {ecmaVersion: 'latest'}), names = new Set();
  localWalk(program, node => {
    if (['FunctionDeclaration', 'ClassDeclaration'].includes(node.type)) names.add(node.id.name);
    if (node.type === 'VariableDeclarator' && ['FunctionExpression', 'ArrowFunctionExpression', 'ClassExpression'].includes(node.init?.type)) names.add(node.id.name);
  });
  let size = -1;
  while (size !== names.size) {
    size = names.size;
    localWalk(program, node => {
      if (node.type === 'VariableDeclarator' && node.init?.type === 'Identifier' && names.has(node.init.name)) names.add(node.id.name);
    });
  }
  localWalk(program, node => {
    if (node.type !== 'MemberExpression') return;
    const property = node.computed ? node.property.value : node.property.name;
    if (['name', 'prototype', 'constructor'].includes(property) ||
        property === 'toString' && node.object.type === 'Identifier' && names.has(node.object.name)) throw Error('Reflective model dependency');
  });
}

function modelInterface(binding, node, context, definition, references, program) {
  if (definition.init.arguments.length || node?.type !== 'ClassDeclaration') throw Error('Unsupported model interface');
  const positions = new Set(references.map(reference => reference.start.pos)), roots = new Set();
  localWalk(program, part => {
    if (part.type === 'MemberExpression' && positions.has(part.object.start)) {
      if (part.computed || part.property.type !== 'Identifier') throw Error('Dynamic model interface');
      roots.add(part.property.name);
    }
  });
  if (!roots.size) throw Error('Unrecognized model interface');
  for (const name of roots) if (!node.body.body.some(member => member.key?.name === name)) throw Error('Missing model interface member');
  const members = modelMembers(node, roots, false).map(member => context.slice(member.start, member.end)).join('\n');
  return 'const ' + binding + '=new class{' + members + '};';
}

function modelStructure(input, factory, definitions) {
  const classes = [...definitions.values()].filter(node => node.type === 'ClassDeclaration');
  const models = classes.filter(node => node.body.body.some(member => member.key?.name === 'InitFromProto'));
  if (models.length !== 1) throw Error('Ambiguous wake model');
  const model = models[0], roots = new Set(['constructor', 'InitFromProto', 'BHasObservableChange',
    'BHasNonObservableChange', 'BHasObservables', 'GetPreservedState', 'RestorePreservedState', 'GetStoreTags', 'local_per_client_data']);
  if (model.superClass) throw Error('Unsupported base model inheritance');
  const init = model.body.body.find(member => member.key?.name === 'InitFromProto');
  const schemas = new Set();
  localWalk(init, node => {if (node.type === 'ForInStatement' && node.right.type === 'Identifier') schemas.add(node.right.name);});
  if (schemas.size !== 1) throw Error('Ambiguous model schema');
  const schema = definitions.get([...schemas][0])?.init;
  if (schema?.type !== 'ObjectExpression') throw Error('Unsupported model schema');
  for (const property of schema.properties) {
    if (property.computed || property.type !== 'Property') throw Error('Dynamic model schema');
    roots.add(property.key.name || property.key.value);
    localWalk(property.value, node => {
      if (node.type === 'CallExpression' && node.callee.type === 'MemberExpression' && !node.callee.computed) roots.add(node.callee.property.name);
    });
  }
  const selected = modelMembers(model, roots, true);
  const subclasses = classes.filter(node => node.superClass?.type === 'Identifier' && node.superClass.name === model.id.name);
  if (subclasses.length !== 1) throw Error('Ambiguous observable model');
  const neutral = modelDataFields(model, schema, [...selected, subclasses[0]]);
  const members = selected.filter(member => !neutral.has(member.key?.name)).map(member => input.slice(member.start, member.end)).join('\n');
  const body = 'class ' + model.id.name + '{' + members + '}\n' + input.slice(subclasses[0].start, subclasses[0].end) + '\nreturn ' + model.id.name + ';';
  const seen = new Set([model.id.name, subclasses[0].id.name, ...factory.params.map(parameter => parameter.name)]);
  const schemaName = [...schemas][0];
  const properties = schema.properties.filter(property => !neutral.has(property.key.name || property.key.value));
  const schemaCode = 'const ' + schemaName + '={' + properties.map(property => input.slice(property.start, property.end)).join(',') + '};';
  return {body, seen, schemaName, selected, overrides: new Map([[schemaName, schemaCode]])};
}

function modelDataFields(model, schema, operations) {
  const referenced = new Set();
  for (const operation of operations) localWalk(operation, node => {
    if (node.type === 'MemberExpression' && node.object.type === 'ThisExpression' &&
        (!node.computed || node.property.type === 'Literal')) referenced.add(node.computed ? node.property.value : node.property.name);
  });
  const fields = new Set(model.body.body.filter(member => member.type === 'PropertyDefinition' &&
    !member.computed && !member.static && !member.value && !referenced.has(member.key.name) &&
    model.body.body.filter(other => other.key?.name === member.key.name).length === 1).map(member => member.key.name));
  return new Set(schema.properties.filter(property => property.value.type === 'ObjectExpression' &&
    property.value.properties.length === 0 && fields.has(property.key.name || property.key.value) &&
    schema.properties.filter(other => (other.key.name || other.key.value) === (property.key.name || property.key.value)).length === 1)
    .map(property => property.key.name || property.key.value));
}

function modelDependencies(input, definitions, state) {
  let body = state.body;
  const seen = state.seen;
  for (let iteration = 0; iteration < 40; iteration++) {
    const program = '(()=>{' + body + '})';
    const globals = modelGlobals(program).filter(([name]) => definitions.has(name) && !seen.has(name));
    if (!globals.length) break;
    for (const [name, definition] of globals) {
      seen.add(name);
      const node = definitions.get(name);
      let declaration = state.overrides?.get(name) || input.slice(node.start, node.end);
      if (state.overrides?.has(name)) {
        // Generic, unobserved data fields are schema parameters, not new algorithms.
      } else if (node.type === 'VariableDeclarator' && node.init?.type === 'NewExpression' && definitions.has(node.init.callee.name)) {
        seen.add(node.init.callee.name);
        declaration = modelInterface(name, definitions.get(node.init.callee.name), input, node, definition.references,
          acorn.parse(program, {ecmaVersion: 'latest'}));
      } else if (node.type === 'VariableDeclarator') declaration = 'const ' + declaration + ';';
      if (declaration.length > 40000 || node.type === 'ClassDeclaration') throw Error('Unsupported model dependency');
      body = declaration + '\n' + body;
    }
  }
  return body;
}

function modelContract(context, outerBindings = [], wake = false) {
  if (context.length > 800000) throw Error('Model scope exceeds limit');
  const input = '(' + context + ')';
  const factory = acorn.parse(input, {ecmaVersion: 'latest'}).body[0].expression;
  if (!factory.body?.body || factory.params.some(parameter => parameter.type !== 'Identifier')) throw Error('Unsupported model scope');
  const definitions = modelDefinitions(factory), state = modelStructure(input, factory, definitions);
  let rewritten = {};
  if (wake) {
    const methods = [...definitions.values()].filter(node => node.type === 'ClassDeclaration')
      .flatMap(node => node.body.body.filter(member => member.key?.name === 'UpdateAppOverview'));
    if (methods.length !== 1 || definitions.has('__LLF_Wake__')) throw Error('Ambiguous wake operation');
    rewritten = wakeRewrite(methods[0], input);
    state.body = 'class __LLF_Wake__{' + rewritten.wake_source + '}\n' + state.body;
  }
  const body = modelDependencies(input, definitions, state);
  const code = '(' + input.slice(factory.start, factory.body.start + 1) + body + '})';
  const globals = modelGlobals(code).map(([name]) => name);
  if (globals.some(name => definitions.has(name))) throw Error('Unresolved model dependency');
  if (globals.some(name => outerBindings.includes(name))) throw Error('Shadowed model global');
  modelReflection(code);
  modelScopeGuard(input, factory, [...state.seen].filter(name => definitions.has(name)), state.schemaName, state.selected);
  return {model: Terser.minify_sync(code, {compress: false, mangle: {toplevel: true, nth_identifier: {get: identifier}},
    format: {ascii_only: true, comments: false}}).code, ...rewritten};
}
