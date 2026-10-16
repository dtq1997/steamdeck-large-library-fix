// [Codex] Small edits at stable data and DOM interfaces; surrounding client fixes stay intact.
function themeDocument(doc) {
  if (!doc) return;
  if (doc.__llfThemeState) { doc.__llfThemeState.watch(); return; }
  const selectors = [], changes = [], roots = new Set();
  let observer;
  const pattern = /:root:has\((\.[\w-]+)\s*>\s*div\[data-(?:accent|dull|body-text)-color=[^\]]+\]\)/g;
  try {
    for (const sheet of doc.styleSheets) {
      let rules;
      try { rules = sheet.cssRules; } catch (_) { continue; }
      const pending = [...rules];
      while (pending.length) {
        const rule = pending.pop();
        if (rule.cssRules) { pending.push(...rule.cssRules); continue; }
        if (!rule.selectorText) continue;
        const original = rule.selectorText;
        const replacement = original.replace(pattern, (selector, root) => {
          let index = selectors.indexOf(selector);
          if (index < 0) { index = selectors.push(selector) - 1; roots.add(root); }
          return ':root.llfTheme' + index + '.llfTheme' + index + ':not(llfNever)';
        });
        if (replacement !== original) changes.push([rule, original, replacement]);
      }
    }
    if (!selectors.length) return;
    // Validate every selector before changing any CSS rule.
    selectors.forEach(selector => doc.documentElement.matches(selector));
    for (const [rule, , replacement] of changes) rule.selectorText = replacement;
    const refresh = () => selectors.forEach((selector, index) =>
      doc.documentElement.classList.toggle('llfTheme' + index, doc.documentElement.matches(selector)));
    observer = new doc.defaultView.MutationObserver(records => {
      if (records.some(record => record.type === 'childList')) watch();
      else refresh();
    });
    const watch = () => {
      observer.disconnect();
      // Watch the theme roots, not the thousands of library cover mutations.
      for (const root of roots) for (const parent of doc.querySelectorAll(root)) {
        observer.observe(parent, {childList: true});
        for (const child of parent.children) observer.observe(child, {attributes: true,
          attributeFilter: ['data-accent-color', 'data-dull-color', 'data-body-text-color']});
      }
      refresh();
    };
    doc.__llfTheme = refresh;
    doc.__llfThemeState = {watch};
    watch();
    doc.defaultView.addEventListener('pagehide', () => observer.disconnect(), {once: true});
  } catch (_) {
    if (observer) observer.disconnect();
    for (const [rule, original] of changes) try { rule.selectorText = original; } catch (_) { /* Preserve the original cascade. */ }
    selectors.forEach((_, index) => doc.documentElement.classList.remove('llfTheme' + index));
    delete doc.__llfTheme;
    delete doc.__llfThemeState;
  }
}

function property(node, name) {
  return node?.type === 'MemberExpression' && !node.computed && node.property.name === name;
}

function countOwner(node, parents) {
  for (let owner = parents.get(node); owner; owner = parents.get(owner)) {
    if (!['FunctionDeclaration', 'FunctionExpression'].includes(owner.type)) continue;
    const members = new Set();
    localWalk(owner.body, part => { if (part.type === 'MemberExpression' && !part.computed) members.add(part.property.name); });
    return ['CollectionLabelCount', 'GetAppCountWithToolsFilter'].every(name => members.has(name));
  }
  return false;
}

function countOperation(node, input, parent, parents) {
  if (!property(node, 'length') || node.object.type !== 'CallExpression') return null;
  const call = node.object;
  if (!property(call.callee, 'filter') || !property(call.callee.object, 'visibleApps') || call.arguments.length !== 1) return null;
  const collection = call.callee.object.object, matches = call.arguments[0];
  if (collection.type !== 'Identifier' || !property(matches, 'Matches') || !property(matches.object, 'collectionsAppFilter')) return null;
  const filter = input.slice(matches.object.start, matches.object.end), name = collection.name;
  const optional = call.callee.object.optional;
  const target = parent?.type === 'ChainExpression' ? parent : node;
  if (optional && target.type !== 'ChainExpression' || !countOwner(node, parents)) return null;
  const used = new Set();
  localWalk(parents.get(node), part => { if (part.type === 'Identifier') used.add(part.name); });
  let prefix = '__llf';
  while (used.has(prefix + 'N') || used.has(prefix + 'F')) prefix += '_';
  const n = prefix + 'N', f = prefix + 'F';
  const compute = `(()=>{const ${n}=${name}.llfN;if(${n}!==void 0)return ${n};const ${f}=${filter};` +
    `return ${name}.visibleApps.filter(${f}.MatchesImpl.bind({m_filterSpec:${f}.ToStorageFormat()})).length})()`;
  return {feature: 'count', edits: [{start: target.start, end: target.end,
    replacement: optional ? `(${name}==null?void 0:${compute})` : compute}]};
}

function previewOperation(block, input) {
  if (block.type !== 'BlockStatement') return null;
  const shape = localExpression('$apps.slice().sort(($a,$b)=>$b.minutes_playtime_forever-$a.minutes_playtime_forever).slice(0,13)');
  for (let index = 0; index + 1 < block.body.length; index++) {
    const first = block.body[index], next = block.body[index + 1];
    if (first.type !== 'VariableDeclaration' || first.declarations.length !== 1 || next.type !== 'ExpressionStatement') continue;
    const declaration = first.declarations[0], assignment = next.expression, captures = {};
    if (declaration.id.type !== 'Identifier' || !property(declaration.init, 'visibleApps') ||
        !property(declaration.init.object, 'collection') || assignment.type !== 'AssignmentExpression' || assignment.operator !== '=' ||
        assignment.left.name !== declaration.id.name || !localShape(assignment.right, shape, captures) || captures.$apps !== declaration.id.name) continue;
    const owner = input.slice(declaration.init.object.start, declaration.init.object.end);
    const calculation = input.slice(assignment.right.start, assignment.right.end);
    const root = assignment.right.callee.object.callee.object.callee.object;
    const replacement = owner + '.llfP??' + input.slice(declaration.init.start, declaration.init.end) +
      calculation.slice(root.end - assignment.right.start);
    return {feature: 'preview', edits: [{start: declaration.init.start, end: declaration.init.end, replacement},
                                      {start: next.start, end: next.end, replacement: ''}]};
  }
  return null;
}

function scrollOperation(node) {
  if (node.type !== 'ObjectExpression') return null;
  const properties = new Map(node.properties.filter(item => item.type === 'Property' && !item.computed)
    .map(item => [item.key.name || item.key.value, item.value]));
  if (properties.get('name')?.value !== 'AllCollectionsGrid' ||
      !['childElements', 'scrollElement', 'refItemsPerRow'].every(name => properties.has(name))) return null;
  if (properties.has('scrollIntoViewType')) return properties.get('scrollIntoViewType').value === 1 ? {feature: 'scroll', edits: []} : null;
  return {feature: 'scroll', edits: [{start: node.end - 1, end: node.end, replacement: ',scrollIntoViewType:1}'}]};
}

function themeOperation(node, parent) {
  if (node.type !== 'MethodDefinition' || node.key.name !== 'componentDidMount' || node.computed ||
      parent?.type !== 'ClassBody' || !['GetChildren', 'ComputeLayout', 'StartListening'].every(name =>
        parent.body.some(member => member.type === 'MethodDefinition' && member.key.name === name))) return null;
  let computes = false, earlyReturn = false;
  localWalk(node.value.body, part => {
    if (part.type === 'CallExpression' && property(part.callee, 'ComputeLayout') && part.callee.object.type === 'ThisExpression') computes = true;
    if (part.type === 'ReturnStatement') earlyReturn = true;
  });
  if (!computes || earlyReturn) return null;
  let grid = false;
  localWalk(parent, part => { if (property(part, 'm_elGrid') && part.object.type === 'ThisExpression') grid = true; });
  if (!grid) return null;
  const position = node.value.body.end - 1;
  const theme = Terser.minify_sync('(' + themeDocument.toString() + ')', {compress: false, mangle: true}).code.replace(/;$/, '');
  return {feature: 'theme', edits: [{start: position, end: position + 1,
    replacement: ';(' + theme + ')(this.m_elGrid?.current?.ownerDocument);}'}]};
}

function operationInterfaces(context) {
  const input = '(' + context + ')', tree = acorn.parse(input, {ecmaVersion: 'latest'}), rows = [];
  localWalk(tree, node => {
    if (!['ClassDeclaration', 'ClassExpression'].includes(node.type)) return;
    if (!['GetChildren', 'ComputeLayout', 'StartListening'].every(name => node.body.body.some(member => member.key?.name === name))) return;
    for (const name of ['GetChildren', 'render']) {
      const member = node.body.body.find(part => part.key?.name === name);
      if (member) rows.push({start: member.start - 1, end: member.end - 1});
    }
  });
  if (rows.length !== 2) throw Error('Ambiguous navigation interface');
  return rows;
}

function operationEdits(request) {
  if (request.context.length > 800000) throw Error('Operation scope exceeds limit');
  const input = '(' + request.context + ')', tree = acorn.parse(input, {ecmaVersion: 'latest'}), operations = [];
  if (localBindings(tree, new Set(['MutationObserver']))) throw Error('Shadowed DOM operation');
  const parents = new Map();
  localWalk(tree, (node, parent) => parents.set(node, parent));
  localWalk(tree, (node, parent) => {
    const edit = countOperation(node, input, parent, parents) || previewOperation(node, input) || scrollOperation(node) || themeOperation(node, parent);
    if (edit) operations.push({...edit, edits: edit.edits.map(item => ({...item, start: item.start - 1, end: item.end - 1}))});
  });
  return {operations};
}
