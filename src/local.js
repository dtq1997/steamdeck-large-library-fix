// [Codex] Version-independent local computations; surrounding source is untouched.
function envelope(text) {
  let suffix = '', body = text;
  if (!/^\d+\s*:/.test(body) && body.endsWith('}),')) { suffix = '}),'; body = body.slice(0, -3); }
  else if (body.endsWith(',')) { suffix = ','; body = body.slice(0, -1); }
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

function localWalk(node, visit, parent = null) {
  if (!node || typeof node !== 'object') return;
  if (node.type) visit(node, parent);
  for (const value of Object.values(node)) {
    if (Array.isArray(value)) value.forEach(item => localWalk(item, visit, node));
    else if (value && typeof value === 'object') localWalk(value, visit, node);
  }
}

function wakeCondition(method) {
  const shape = localExpression('!$full&&$app&&!$non&&!$obs'), choices = [];
  localWalk(method.value.body, node => {
    if (node.type !== 'IfStatement' || node.alternate || node.consequent.type !== 'ContinueStatement' || node.consequent.label) return;
    const captures = {};
    if (localShape(node.test, shape, captures)) choices.push({node: node.test, captures});
  });
  if (choices.length !== 1) throw Error('Ambiguous wake skip condition');
  const {node, captures: c} = choices[0];
  return {node, app: c.$app, code: `${c.$app}&&!${c.$non}&&!${c.$obs}&&(!${c.$full}||this.m_bIsInitialized)`};
}

function wakeTagLoop(method, app) {
  const shape = localExpression('$app.GetStoreTags().forEach($tag=>$set.add($tag))'), choices = [];
  localWalk(method.value.body, (node, parent) => {
    const captures = {};
    if (!localShape(node, shape, captures) || captures.$app !== app) return;
    if (parent.type === 'ExpressionStatement') choices.push({start: parent.start, end: parent.end, set: captures.$set});
    else if (parent.type === 'SequenceExpression' && parent.expressions.at(-1) === node && parent.expressions.length > 1) {
      choices.push({start: parent.expressions.at(-2).end, end: node.end, set: captures.$set});
    }
  });
  if (choices.length !== 1) throw Error('Ambiguous unused wake accumulator');
  return choices[0];
}

function wakeSetDeclaration(method, name) {
  const shape = localExpression('new Set'), choices = [];
  let uses = 0;
  localWalk(method.value.body, (node, parent) => {
    if (node.type === 'Identifier' && node.name === name) uses++;
    if (node.type !== 'VariableDeclarator' || node.id.name !== name || !localShape(node.init, shape)) return;
    const declarations = parent.declarations, index = declarations.indexOf(node);
    if (declarations.length === 1) choices.push({start: parent.start, end: parent.end});
    else if (index < declarations.length - 1) choices.push({start: node.start, end: declarations[index + 1].start});
    else choices.push({start: declarations[index - 1].end, end: node.end});
  });
  if (uses !== 2 || choices.length !== 1) throw Error('Wake accumulator is used by other logic');
  return choices[0];
}

function wakeRewrite(method, input) {
  if (method.computed || method.static || method.value.async || method.value.params.length !== 1) throw Error('Unsupported wake method');
  const condition = wakeCondition(method), loop = wakeTagLoop(method, condition.app);
  const declaration = wakeSetDeclaration(method, loop.set);
  const edits = [[condition.node.start, condition.node.end, condition.code], [loop.start, loop.end, ''],
    [declaration.start, declaration.end, '']];
  let replacement = input.slice(method.start, method.end);
  for (const [a, b, text] of edits.sort((a, b) => b[0] - a[0])) {
    replacement = replacement.slice(0, a - method.start) + text + replacement.slice(b - method.start);
  }
  acorn.parse('class __Wake__{' + replacement + '}', {ecmaVersion: 'latest'});
  return {wake_source: input.slice(method.start, method.end), wake_replacement: replacement};
}

function localExpression(text) {
  return acorn.parse('(' + text + ')', {ecmaVersion: 'latest'}).body[0].expression;
}

function localShape(node, shape, captures = {}) {
  if (shape?.type === 'Identifier' && shape.name.startsWith('$')) {
    if (node?.type !== 'Identifier') return false;
    if (captures[shape.name] && captures[shape.name] !== node.name) return false;
    captures[shape.name] = node.name;
    return true;
  }
  if (!shape || typeof shape !== 'object') return node === shape;
  if (!node || typeof node !== 'object') return false;
  if (Array.isArray(shape)) return Array.isArray(node) && node.length === shape.length &&
    shape.every((value, index) => localShape(node[index], value, captures));
  const keys = Object.keys(shape).filter(key => !['start', 'end', 'raw'].includes(key));
  if (Object.keys(node).filter(key => !['start', 'end', 'raw'].includes(key)).length !== keys.length) return false;
  return keys.every(key => localShape(node[key], shape[key], captures));
}

function localBindings(tree, wanted) {
  let unsafe = false;
  const binding = node => localWalk(node, part => {
    if (part.type === 'Identifier' && wanted.has(part.name)) unsafe = true;
  });
  localWalk(tree, node => {
    if (node.type === 'VariableDeclarator') binding(node.id);
    if (node.params) node.params.forEach(binding);
    if (node.type === 'CatchClause') binding(node.param);
    if (['FunctionDeclaration', 'ClassDeclaration', 'ClassExpression', 'FunctionExpression'].includes(node.type)) binding(node.id);
    if (node.type === 'WithStatement' || (node.type === 'CallExpression' && node.callee.name === 'eval')) unsafe = true;
  });
  return unsafe;
}

function collectionExpression(left, right) {
  // Count numeric appids in linear time. Other values retain the original calculation
  // and evaluate the left serialization before reading the right collection.
  return `((a,g)=>{let id=x=>typeof x==="number"&&x===(x>>>0);` +
    `if(!a.every(id))return JSON.stringify(a.sort())!=JSON.stringify(g().sort());` +
    `let b=g();if(!b.every(id))return JSON.stringify(a.sort())!=JSON.stringify(b.sort());` +
    `if(a.length!==b.length)return true;let c={__proto__:null};` +
    `for(let x of a)c[x]=(c[x]||0)+1;for(let x of b)if(!c[x]--)return true;return false})` +
    `(${left},()=>${right})`;
}

function collectionEdit(method, wrapped) {
  if (method.key.name !== 'UpdateApps' || method.value.params.length !== 2 || method.value.async) return null;
  const shape = localExpression('JSON.stringify(Array.from($set.values()).sort())!=JSON.stringify(Array.from(this.m_setApps.values()).sort())');
  const edits = [];
  localWalk(method.value.body, node => {
    const captures = {};
    if (!localShape(node, shape, captures)) return;
    const left = node.left.arguments[0].callee.object;
    const right = node.right.arguments[0].callee.object;
    edits.push({node, code: collectionExpression(wrapped.slice(left.start, left.end), wrapped.slice(right.start, right.end))});
  });
  return edits.length === 1 ? edits[0] : null;
}

function startupEdit(method, wrapped) {
  if (method.key.name !== 'InitAfterLogin' || !method.value.async || method.value.params.length) return null;
  const shape = localExpression('["DynamicUserStore",()=>$module.Fm.Get().HintLoad()]');
  const edits = [];
  localWalk(method.value.body, node => {
    if (!localShape(node, shape)) return;
    const target = node.elements[1].body, call = wrapped.slice(target.start, target.end);
    // Start the original call once before arming the timer, retaining synchronous throws.
    edits.push({node: target, code: `(p=>{let t;return Promise.race([p,new Promise(r=>{t=setTimeout(r,8e3)})]).finally(()=>clearTimeout(t))})(${call})`});
  });
  return edits.length === 1 ? edits[0] : null;
}

function localTransform(request) {
  if (request.context.length > 800000) throw Error('Local scope exceeds limit');
  const wrapped = '({' + request.source + '})';
  const object = acorn.parse(wrapped, {ecmaVersion: 'latest'}).body[0].expression;
  const method = object.properties[0];
  if (object.properties.length !== 1 || method.type !== 'Property' || !method.method || method.computed) return {};
  const startup = method.key.name === 'InitAfterLogin';
  const names = new Set(startup ? ['Promise', 'setTimeout', 'clearTimeout'] : ['JSON', 'Array']);
  const context = acorn.parse('(' + request.context + ')', {ecmaVersion: 'latest'});
  if ((request.outer_bindings || []).some(name => names.has(name)) || localBindings(context, names)) return {};
  const edit = startup ? startupEdit(method, wrapped) : collectionEdit(method, wrapped);
  if (!edit) return {};
  const start = edit.node.start - 2, end = edit.node.end - 2;
  acorn.parse('({' + request.source.slice(0, start) + edit.code + request.source.slice(end) + '})', {ecmaVersion: 'latest'});
  return {feature: startup ? 'startup' : 'collections', edits: [{start, end, replacement: edit.code}]};
}
