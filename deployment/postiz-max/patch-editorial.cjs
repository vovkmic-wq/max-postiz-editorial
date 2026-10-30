const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const hash = value => crypto.createHash('sha256').update(value).digest('hex');

const copies = {
  'editorial.controller.ts': 'apps/backend/src/api/routes/editorial.controller.ts',
  'editorial.page.tsx': 'apps/frontend/src/app/(app)/(site)/editorial/page.tsx',
  'editorial-panel.tsx': 'apps/frontend/src/components/editorial/editorial-panel.tsx',
  'editorial-form.ts': 'apps/frontend/src/components/editorial/editorial-form.ts',
  'editorial.module.css': 'apps/frontend/src/components/editorial/editorial.module.css',
};
function replaceOnce(text, anchor, replacement) {
  if (text.split(anchor).length !== 2) throw new Error('EDITORIAL_PATCH_ANCHOR_MISMATCH');
  return text.replace(anchor, replacement);
}
function planPatch(root, adapter = __dirname, manifest = JSON.parse(fs.readFileSync(path.join(adapter, 'editorial-manifest.json')))) {
  // Сначала проверяем весь исходный снимок: при несовпадении ничего не записываем.
  for (const [relative, expected] of Object.entries({ ...manifest.sources, ...manifest.compiled })) {
    if (hash(fs.readFileSync(path.join(root, relative))) !== expected) throw new Error('EDITORIAL_PINNED_BASE_MISMATCH');
  }
  const plans = [];
  const apiPath = 'apps/backend/src/api/api.module.ts';
  let api = fs.readFileSync(path.join(root, apiPath), 'utf8');
  const nl = api.includes('\r\n') ? '\r\n' : '\n';
  api = `import { EditorialController } from './routes/editorial.controller';${nl}` + api;
  api = replaceOnce(api, 'const authenticatedController = [', `const authenticatedController = [${nl}  EditorialController,`);
  plans.push([apiPath, api]);
  const menuPath = 'apps/frontend/src/components/layout/top.menu.tsx';
  let menu = fs.readFileSync(path.join(root, menuPath), 'utf8');
  menu = replaceOnce(menu, '  const firstMenu = [', `  const firstMenu = [\n    {\n      name: 'Редакция',\n      path: '/editorial',\n      role: ['SUPERADMIN'],\n      icon: (<svg width="21" height="23" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 3h14v18H5zM8 7h8M8 11h8M8 15h5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /></svg>),\n    },`);
  plans.push([menuPath, menu]);
  for (const [source, destination] of Object.entries(copies)) {
    if (fs.existsSync(path.join(root, destination))) throw new Error('EDITORIAL_TARGET_ALREADY_EXISTS');
    plans.push([destination, fs.readFileSync(path.join(adapter, source), 'utf8')]);
  }
  return { plans, manifest };
}
function compilePlans(root, planned, ts) {
  if (ts.version !== planned.manifest.typescript_version) throw new Error('EDITORIAL_COMPILER_VERSION_MISMATCH');
  const sourceApi = planned.plans.find(([name]) => name === 'apps/backend/src/api/api.module.ts')[1];
  const sourceController = planned.plans.find(([name]) => name === copies['editorial.controller.ts'])[1];
  const additions = [];
  for (const [relative, source] of [
    ['apps/backend/dist/apps/backend/src/api/api.module.js', sourceApi],
    ['apps/backend/dist/apps/backend/src/api/routes/editorial.controller.js', sourceController],
  ]) {
    if (relative.endsWith('editorial.controller.js') && fs.existsSync(path.join(root, relative))) throw new Error('EDITORIAL_TARGET_ALREADY_EXISTS');
    const compiled = ts.transpileModule(source, { fileName: relative.replace(/\.js$/, '.ts'), reportDiagnostics: true,
      compilerOptions: { target: ts.ScriptTarget.ES2017, module: ts.ModuleKind.CommonJS,
        experimentalDecorators: true, emitDecoratorMetadata: true, importHelpers: true, esModuleInterop: true, sourceMap: false } });
    if ((compiled.diagnostics || []).some(item => item.category === ts.DiagnosticCategory.Error)) throw new Error('EDITORIAL_TYPESCRIPT_INVALID');
    let output = compiled.outputText;
    const aliases = {
      '@gitroom/backend/': 'apps/backend/dist/apps/backend/src/',
      '@gitroom/nestjs-libraries/': 'apps/backend/dist/libraries/nestjs-libraries/src/',
    };
    output = output.replace(/require\("(@gitroom\/[^"\n]+)"\)/g, (expression, specifier) => {
      const found = Object.entries(aliases).find(([alias]) => specifier.startsWith(alias));
      if (!found) throw new Error('EDITORIAL_UNRESOLVED_ALIAS');
      const [alias, target] = found;
      const imported = path.join(root, target, specifier.slice(alias.length));
      const relativeImport = path.relative(path.dirname(path.join(root, relative)), imported).split(path.sep).join('/');
      return `require("${relativeImport.startsWith('.') ? '' : './'}${relativeImport}")`;
    });
    if (output.includes('@gitroom/')) throw new Error('EDITORIAL_UNRESOLVED_ALIAS');
    additions.push([relative, output]);
  }
  return [...planned.plans, ...additions];
}
function applyPatch(root, adapter, ts) {
  const planned = planPatch(root, adapter);
  const all = compilePlans(root, planned, ts);
  for (const [relative, content] of all) {
    const destination = path.join(root, relative);
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.writeFileSync(destination, content);
  }
  return { base_image_id: planned.manifest.base_image_id, files: Object.fromEntries(all.map(([relative, content]) => [relative, hash(content)])) };
}
module.exports = { planPatch, compilePlans, applyPatch, hash };
if (require.main === module) {
  const root = process.argv[2] || '/app';
  const ts = require(path.join(root, 'node_modules/typescript'));
  const receipt = applyPatch(root, __dirname, ts);
  fs.writeFileSync(path.join(__dirname, 'editorial-receipt.json'), JSON.stringify(receipt, null, 2));
  console.log('Редакция: добавлены защищённый BFF и интерфейс; остальные backend-модули сохранены.');
}
