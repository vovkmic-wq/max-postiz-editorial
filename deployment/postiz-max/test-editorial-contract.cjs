const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const tooling = process.env.EDITORIAL_TOOLING || (fs.existsSync('/app/node_modules/typescript') ? '/app/node_modules' : path.resolve(__dirname, '../../data/postiz-editorial-tooling/node_modules'));
const ts = require(path.join(tooling, 'typescript'));
const { planPatch, compilePlans, hash } = require('./patch-editorial.cjs');
const options = { target: ts.ScriptTarget.ES2017, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  experimentalDecorators: true, emitDecoratorMetadata: true, esModuleInterop: true };
function loadModule(filename, dependencies = {}, globals = {}) {
  const result = ts.transpileModule(fs.readFileSync(path.join(__dirname, filename), 'utf8'), { fileName: filename, compilerOptions: options, reportDiagnostics: true });
  assert.deepEqual((result.diagnostics || []).filter(d => d.category === ts.DiagnosticCategory.Error), []);
  const module = { exports: {} };
  const context = vm.createContext({ module, exports: module.exports, URL, AbortController, setTimeout, clearTimeout,
    require(name) { if (name in dependencies) return dependencies[name]; throw new Error(`Unexpected dependency ${name}`); }, ...globals });
  vm.runInContext(result.outputText, context, { filename });
  return module.exports;
}
class HttpException extends Error { constructor(body, status) { super(body.code); this.body = body; this.status = status; } }
const decorator = () => () => undefined;
const nest = { HttpException, Controller: decorator, Get: decorator, HttpCode: decorator, Put: decorator, Post: decorator, Req: decorator, Body: decorator };
const env = {
  EDITORIAL_OWNER_USER_ID: 'owner-id', EDITORIAL_OWNER_ORGANIZATION_ID: 'org-id',
  EDITORIAL_INTERNAL_URL: 'http://host.docker.internal:8092/internal/editorial', EDITORIAL_INTERNAL_KEY: 'private-test-key-not-real-00000000',
  FRONTEND_URL: 'http://postiz.test:4007',
};
const request = () => ({ headers: { origin: env.FRONTEND_URL, 'content-type': 'application/json' }, cookies: { auth: 'session' },
  user: { id: 'owner-id', activated: true }, org: { id: 'org-id', users: [{ role: 'SUPERADMIN', disabled: false }] } });
function controller(fetchImpl, overrides = {}) {
  const calls = [];
  const Controller = loadModule('editorial.controller.ts', { '@nestjs/common': nest }, {
    process: { env: { ...env, ...overrides } }, fetch: async (...args) => { calls.push(args); return fetchImpl ? fetchImpl(...args) : response({ revision: 2, paused: true }); },
  }).EditorialController;
  return { instance: new Controller(), calls };
}
const response = (body, status = 200) => ({ ok: status < 300, status, text: async () => JSON.stringify(body) });
const rejected = (promise, code, status) => assert.rejects(promise, error => error.message === code && (!status || error.status === status));

test('cookie owner request forwards fixed path and trusted identity, never browser credentials', async () => {
  const { instance, calls } = controller();
  const req = request(); req.headers['x-editorial-owner'] = 'attacker'; req.headers.cookie = 'secret browser cookie';
  const result = await instance.state(req);
  assert.equal(result.paused, true); assert.equal(calls.length, 1);
  assert.equal(calls[0][0], `${env.EDITORIAL_INTERNAL_URL}/state`);
  assert.equal(calls[0][1].headers['X-Editorial-Owner'], 'owner-id');
  assert.equal(calls[0][1].headers['X-Editorial-Organization'], 'org-id');
  assert.equal(calls[0][1].headers['X-Editorial-Key'], env.EDITORIAL_INTERNAL_KEY);
  assert.equal(JSON.stringify(calls).includes('secret browser cookie'), false);
  assert.equal(calls[0][1].redirect, 'error');
});
for (const [name, mutate, code] of [
  ['no cookie', req => { req.cookies = {}; }, 'EDITORIAL_COOKIE_SESSION_REQUIRED'],
  ['auth header', req => { req.headers.auth = 'jwt'; }, 'EDITORIAL_COOKIE_SESSION_REQUIRED'],
  ['bearer with cookie', req => { req.headers.authorization = 'Bearer x'; }, 'EDITORIAL_COOKIE_SESSION_REQUIRED'],
  ['impersonation', req => { req.cookies.impersonate = 'other'; }, 'EDITORIAL_COOKIE_SESSION_REQUIRED'],
  ['other owner', req => { req.user.id = 'other'; }, 'EDITORIAL_OWNER_REQUIRED'],
  ['inactive user', req => { req.user.activated = false; }, 'EDITORIAL_OWNER_REQUIRED'],
  ['other organization', req => { req.org.id = 'other'; }, 'EDITORIAL_OWNER_REQUIRED'],
  ['nonowner role', req => { req.org.users[0].role = 'ADMIN'; }, 'EDITORIAL_OWNER_REQUIRED'],
  ['disabled membership', req => { req.org.users[0].disabled = true; }, 'EDITORIAL_OWNER_REQUIRED'],
  ['ambiguous membership', req => { req.org.users.push(req.org.users[0]); }, 'EDITORIAL_OWNER_REQUIRED'],
  ['foreign origin', req => { req.headers.origin = 'https://evil.test'; }, 'EDITORIAL_ORIGIN_REQUIRED'],
]) test(`BFF blocks ${name} before internal request`, async () => {
  const { instance, calls } = controller(); const req = request(); mutate(req);
  await rejected(instance.state(req), code); assert.equal(calls.length, 0);
});
test('same-origin browser GET without Origin works, mutations without Origin do not', async () => {
  const { instance, calls } = controller(); const req = request(); delete req.headers.origin; req.headers['sec-fetch-site'] = 'same-origin';
  await instance.settings(req);
  await rejected(Promise.resolve().then(() => instance.control(req, { action: 'pause' })), 'EDITORIAL_ORIGIN_REQUIRED');
  assert.equal(calls.length, 1);
});
test('HTTP LAN GET accepts same-origin Referer without fetch metadata and rejects foreign/missing Referer', async () => {
  const { instance, calls } = controller(); const req = request(); delete req.headers.origin;
  req.headers.referer = `${env.FRONTEND_URL}/editorial`;
  await instance.state(req);
  req.headers.referer = 'http://evil.test/editorial';
  await rejected(instance.state(req), 'EDITORIAL_ORIGIN_REQUIRED');
  delete req.headers.referer;
  await rejected(instance.state(req), 'EDITORIAL_ORIGIN_REQUIRED');
  assert.equal(calls.length, 1);
});
test('MAX browser authorization checks owner session only and makes zero Python calls', async () => {
  const { instance, calls } = controller();
  assert.equal(instance.browserAuthorize(request()), undefined);
  const req = request(); req.user.id = 'other';
  await rejected(Promise.resolve().then(() => instance.browserAuthorize(req)), 'EDITORIAL_OWNER_REQUIRED');
  assert.equal(calls.length, 0);
  const source = fs.readFileSync(path.join(__dirname, 'editorial.controller.ts'), 'utf8');
  assert.ok(source.includes("@Get('/browser-authorize')\n  @HttpCode(204)"));
});
test('settings/control enforce envelopes and revision, no publish endpoint exists', async () => {
  const { instance, calls } = controller(); const req = request();
  await instance.updateSettings(req, { revision: 3, settings: { source: 'nasa_svs' } });
  assert.equal(calls[0][1].method, 'PUT'); assert.deepEqual(JSON.parse(calls[0][1].body), { revision: 3, settings: { source: 'nasa_svs' } });
  await instance.control(req, { action: 'pause', revision: 3 });
  assert.deepEqual(JSON.parse(calls[1][1].body), { action: 'pause', revision: 3 });
  for (const bad of [{ revision: true, settings: {} }, { revision: 2, settings: [], approval: true }, { revision: -1, settings: {} }]) {
    await rejected(Promise.resolve().then(() => instance.updateSettings(req, bad)), 'EDITORIAL_SETTINGS_INVALID', 422);
  }
  await rejected(Promise.resolve().then(() => instance.control(req, { action: 'publish' })), 'EDITORIAL_CONTROL_INVALID', 422);
  for (const revision of [undefined, 0, true, -1, 1.2]) {
    await rejected(Promise.resolve().then(() => instance.control(req, { action: 'pause', revision })), 'EDITORIAL_CONTROL_INVALID', 422);
  }
  assert.equal(calls.length, 2); assert.equal(instance.publish, undefined);
});
for (const internalUrl of ['https://evil.test/internal/editorial', 'http://host.docker.internal:8092/other',
  'http://user:pass@localhost:8092/internal/editorial', 'http://127.0.0.1:8092/internal/editorial?next=evil', 'http://localhost:8090/internal/editorial']) {
  test(`internal destination rejected: ${new URL(internalUrl).origin}`, async () => {
    const { instance, calls } = controller(undefined, { EDITORIAL_INTERNAL_URL: internalUrl });
    await rejected(instance.state(request()), 'EDITORIAL_NOT_CONFIGURED', 503); assert.equal(calls.length, 0);
  });
}
test('upstream conflicts are sanitized; network and nonJSON errors fail closed', async () => {
  const conflicting = controller(() => response({ code: 'EDITORIAL_REVISION_CONFLICT', message: 'private-stack' }, 409));
  await rejected(conflicting.instance.state(request()), 'EDITORIAL_REVISION_CONFLICT', 409);
  const network = controller(() => { throw new Error('internal-secret-host'); });
  await rejected(network.instance.state(request()), 'EDITORIAL_SERVICE_UNAVAILABLE', 503);
  const html = controller(() => ({ ok: false, status: 500, text: async () => '<html>private stack</html>' }));
  await rejected(html.instance.state(request()), 'EDITORIAL_RESPONSE_INVALID', 502);
});
test('mutations require JSON and missing owner configuration makes zero calls', async () => {
  const first = controller(); const req = request(); req.headers['content-type'] = 'text/plain';
  await rejected(Promise.resolve().then(() => first.instance.control(req, { action: 'pause' })), 'EDITORIAL_JSON_REQUIRED', 415);
  const second = controller(undefined, { EDITORIAL_OWNER_USER_ID: '' });
  await rejected(second.instance.state(request()), 'EDITORIAL_NOT_CONFIGURED', 503);
  assert.equal(first.calls.length + second.calls.length, 0);
});
test('short internal key is rejected before any internal request', async () => {
  const { instance, calls } = controller(undefined, { EDITORIAL_INTERNAL_KEY: 'short' });
  await rejected(instance.state(request()), 'EDITORIAL_NOT_CONFIGURED', 503); assert.equal(calls.length, 0);
});
const formApi = loadModule('editorial-form.ts');
test('new UI/controller modules pass strict semantic TypeScript with installed React types', () => {
  const stubPath = path.join(__dirname, 'editorial-type-stubs.d.ts');
  const stubs = `declare module '@nestjs/common' {
    export class HttpException extends Error { constructor(body: any, status: number); }
    export function Body(...args: any[]): any; export function Controller(...args: any[]): any;
    export function Get(...args: any[]): any; export function HttpCode(...args: any[]): any; export function Post(...args: any[]): any;
    export function Put(...args: any[]): any; export function Req(...args: any[]): any;
  }
  declare module '*.module.css' { const styles: Record<string, string>; export default styles; }
  declare module 'next' { export interface Metadata { title?: string; } }`;
  const compilerOptions = { ...options, strict: true, noEmit: true, skipLibCheck: true,
    moduleResolution: ts.ModuleResolutionKind.Node10, lib: ['lib.es2022.d.ts', 'lib.dom.d.ts'],
    typeRoots: [path.join(tooling, '@types')], types: ['node', 'react'], baseUrl: __dirname,
    paths: { react: [path.join(tooling, '@types/react/index.d.ts')], 'react/jsx-runtime': [path.join(tooling, '@types/react/jsx-runtime.d.ts')],
      '@gitroom/frontend/components/editorial/editorial-panel': ['./editorial-panel.tsx'] } };
  const host = ts.createCompilerHost(compilerOptions), originalRead = host.readFile, originalExists = host.fileExists, originalSource = host.getSourceFile;
  const isStub = filename => path.resolve(filename) === path.resolve(stubPath);
  host.readFile = filename => isStub(filename) ? stubs : originalRead(filename);
  host.fileExists = filename => isStub(filename) || originalExists(filename);
  host.getSourceFile = (filename, languageVersion, onError, shouldCreateNewSourceFile) => isStub(filename)
    ? ts.createSourceFile(filename, stubs, languageVersion) : originalSource(filename, languageVersion, onError, shouldCreateNewSourceFile);
  const program = ts.createProgram(['editorial.controller.ts', 'editorial-form.ts', 'editorial-panel.tsx', 'editorial.page.tsx'].map(name => path.join(__dirname, name)).concat(stubPath), compilerOptions, host);
  const diagnostics = ts.getPreEmitDiagnostics(program).filter(item => item.category === ts.DiagnosticCategory.Error);
  assert.deepEqual(diagnostics.map(item => ts.flattenDiagnosticMessageText(item.messageText, '\n')), []);
});
const initial = { source: 'nasa_svs', interval_seconds: 3600, opaque_server_option: 'keep',
  nasa: { authors: [], keywords: [], min_rating: 0, max_words: 500 },
  reddit: { subreddits: [], authors: [], keywords: [], min_score: 0, min_comments: 0, max_age_hours: 72, limit: 25, sort: 'top', time_filter: 'week', max_pages: 3 } };
test('structured form roundtrip preserves server-only bounds and maps Russian rating to min_score', () => {
  const form = formApi.formFromSettings(initial); form.source = 'reddit'; form.subreddits = 'science, space\nscience'; form.redditRating = '150';
  const result = formApi.settingsFromForm(initial, form);
  assert.deepEqual(JSON.parse(JSON.stringify(result.reddit.subreddits)), ['science', 'space']);
  assert.equal(result.reddit.min_score, 150); assert.equal(result.reddit.min_rating, undefined);
  assert.equal(result.opaque_server_option, 'keep'); assert.equal(result.reddit.max_pages, 3); assert.equal(result.nasa.max_words, 500);
  assert.equal(result.nasa.min_rating, 0);
});
for (const [field, value] of [['interval', '59'], ['interval', '60.5'], ['redditRating', '-1'], ['redditRating', '1000001'], ['redditAge', '0'], ['redditLimit', '101'], ['redditSort', 'unknown']]) {
  test(`form rejects invalid ${field}=${value}`, () => { const form = formApi.formFromSettings(initial); form[field] = value; assert.throws(() => formApi.settingsFromForm(initial, form), /EDITORIAL_FORM_INVALID/); });
}
test('Reddit requires authors or subreddit, never invents access authorization', () => {
  const form = formApi.formFromSettings(initial); form.source = 'reddit';
  assert.throws(() => formApi.settingsFromForm(initial, form), /EDITORIAL_FORM_INVALID/);
  form.redditAuthors = 'example'; assert.equal(formApi.settingsFromForm(initial, form).source, 'reddit');
});
test('native React records escape titles, reject unsafe URLs, loading page has Russian controls', () => {
  const React = require(path.join(tooling, 'react'));
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const ui = loadModule('editorial-panel.tsx', { react: React, 'react/jsx-runtime': require(path.join(tooling, 'react/jsx-runtime')),
    './editorial-form': formApi, './editorial.module.css': new Proxy({}, { get: (_, key) => String(key) }) });
  const markup = renderToStaticMarkup(React.createElement(ui.EditorialRecords, { records: [{ id: 'id', title: '<script>bad</script>', url: 'javascript:alert(1)' }] }));
  assert.ok(markup.includes('&lt;script&gt;')); assert.ok(!markup.includes('href='));
  const page = renderToStaticMarkup(React.createElement(ui.EditorialPanel));
  for (const text of ['Редакция', 'Запустить цикл', 'Поставить на паузу', 'Последние материалы', 'Вход в MAX', '/owner-browser/vnc.html?autoconnect=1&amp;resize=scale']) assert.ok(page.includes(text));
  assert.ok(!page.includes('iframe')); assert.ok(!page.includes('private-test-key'));
  const nasa = renderToStaticMarkup(React.createElement(ui.NasaRatingField));
  assert.ok(nasa.includes('Источник не предоставляет оценку')); assert.ok(nasa.includes('disabled=""')); assert.ok(nasa.includes('value="0"'));
  for (const phase of ['selecting', 'preparing', 'scheduling', 'waiting_dispatch', 'reconciling', 'rejected', 'hour_used', 'no_source', 'selection_slot_expired', 'waiting_interval']) {
    assert.notEqual(ui.phaseText(phase), phase); assert.match(ui.phaseText(phase), /[А-Яа-яЁё]/);
  }
  for (const code of ['NASA_RATING_UNAVAILABLE', 'EDITORIAL_SOURCE_POLICY_REQUIRED', 'ARTICLE_RECONCILIATION_REQUIRED', 'EDITORIAL_LEADERSHIP_LOST', 'REDDIT_COLLECTION_CONFIG_INVALID', 'REDDIT_COLLECTION_SCOPE_REQUIRED']) {
    assert.ok(!ui.errorText(code).includes(code)); assert.match(ui.errorText(code), /[А-Яа-яЁё]/);
  }
  assert.notEqual(ui.readableTime(1761080400), '—'); assert.equal(ui.readableTime(undefined), '—');
});
test('MAX login link supplies the exact root-relative noVNC WebSocket proxy path', () => {
  const React = require(path.join(tooling, 'react'));
  const ui = loadModule('editorial-panel.tsx', { react: React, 'react/jsx-runtime': require(path.join(tooling, 'react/jsx-runtime')),
    './editorial-form': formApi, './editorial.module.css': new Proxy({}, { get: (_, key) => String(key) }) });
  const url = new URL(ui.OWNER_BROWSER_URL, 'http://postiz.test:4007');
  assert.equal(url.pathname, '/owner-browser/vnc.html');
  assert.equal(url.searchParams.get('autoconnect'), '1'); assert.equal(url.searchParams.get('resize'), 'scale');
  assert.equal(url.searchParams.get('path'), 'owner-browser/websockify');
  // Установленный noVNC добавляет '/' перед path, а не перед каталогом vnc.html.
  assert.equal(new URL('/' + url.searchParams.get('path'), url.origin).pathname, '/owner-browser/websockify');
  const source = fs.readFileSync(path.join(__dirname, 'editorial-panel.tsx'), 'utf8');
  assert.ok(source.includes('path=owner-browser/websockify')); assert.ok(source.includes('href={OWNER_BROWSER_URL}'));
});
function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'postiz-editorial-'));
  const api = `import { Module } from '@nestjs/common';\nimport { ExistingController } from '@gitroom/backend/api/routes/existing.controller';\nconst authenticatedController = [ExistingController];\nexport class ApiModule { configure(consumer: any) { consumer.apply('AuthMiddleware').forRoutes(...authenticatedController); } }`;
  const menu = 'export const useMenuItem = () => {\n  const firstMenu = [\n  ]; return firstMenu; };';
  const files = { 'apps/backend/src/api/api.module.ts': api, 'apps/frontend/src/components/layout/top.menu.tsx': menu,
    'apps/backend/dist/apps/backend/src/api/api.module.js': 'pinned compiled API', 'unrelated-guard.js': 'CAS sentinel unchanged' };
  for (const [relative, content] of Object.entries(files)) { fs.mkdirSync(path.dirname(path.join(root, relative)), { recursive: true }); fs.writeFileSync(path.join(root, relative), content); }
  const manifest = { typescript_version: ts.version, sources: Object.fromEntries(Object.entries(files).filter(([name]) => name.endsWith('.ts') || name.endsWith('.tsx')).map(([name, content]) => [name, hash(content)])),
    compiled: { 'apps/backend/dist/apps/backend/src/api/api.module.js': hash('pinned compiled API') } };
  return { root, manifest };
}
test('strict overlay registers controller inside authenticated routes and compiles only two backend modules', () => {
  const { root, manifest } = fixture();
  try {
    const plan = planPatch(root, __dirname, manifest); const all = compilePlans(root, plan, ts);
    const api = all.find(([name]) => name.endsWith('api.module.ts'))[1];
    assert.ok(api.includes('const authenticatedController = [\n  EditorialController,')); assert.ok(api.includes('forRoutes(...authenticatedController)'));
    const compiled = all.filter(([name]) => name.endsWith('.js')); assert.equal(compiled.length, 2);
    assert.ok(compiled[0][1].includes('require("./routes/editorial.controller")'));
    assert.ok(compiled[0][1].includes('require("./routes/existing.controller")')); assert.ok(!compiled[0][1].includes('@gitroom/'));
    assert.equal(fs.readFileSync(path.join(root, 'unrelated-guard.js'), 'utf8'), 'CAS sentinel unchanged');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
test('pinned mismatch and preexisting route fail before any planned source writes', () => {
  const { root, manifest } = fixture();
  try {
    fs.writeFileSync(path.join(root, 'apps/backend/src/api/api.module.ts'), 'unexpected source');
    assert.throws(() => planPatch(root, __dirname, manifest), /EDITORIAL_PINNED_BASE_MISMATCH/);
    assert.equal(fs.existsSync(path.join(root, 'apps/backend/src/api/routes/editorial.controller.ts')), false);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
test('preexisting native route is never overwritten', () => {
  const { root, manifest } = fixture();
  try {
    const existing = path.join(root, 'apps/frontend/src/app/(app)/(site)/editorial/page.tsx');
    fs.mkdirSync(path.dirname(existing), { recursive: true }); fs.writeFileSync(existing, 'existing owner route');
    assert.throws(() => planPatch(root, __dirname, manifest), /EDITORIAL_TARGET_ALREADY_EXISTS/);
    assert.equal(fs.readFileSync(existing, 'utf8'), 'existing owner route');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
test('image preflight refuses wrong base and never starts or replaces containers', () => {
  const { main, buildArguments } = require('./build-editorial-image.cjs');
  const args = ['--origin', 'http://postiz.test:4007', '--tag', 'max-postiz-linux:editorial-test'];
  const calls = [], manifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'editorial-manifest.json')));
  assert.throws(() => main(args, () => ({ status: 0, stdout: 'wrong-image' })), /EDITORIAL_BUILD_BASE_IMAGE_MISMATCH/);
  main(args, (binary, parameters) => { calls.push([binary, parameters]); return { status: 0, stdout: manifest.base_image_id }; });
  assert.equal(calls.length, 1); assert.deepEqual(calls[0][1].slice(0, 2), ['image', 'inspect']);
  main([...args, '--build'], (binary, parameters) => { calls.push([binary, parameters]); return { status: 0, stdout: manifest.base_image_id }; });
  assert.equal(calls.length, 3); assert.equal(calls[2][1][0], 'build');
  assert.ok(!calls.flat(2).some(value => ['run', 'up', 'restart', 'stop'].includes(value)));
  assert.throws(() => buildArguments(['--origin', 'http://postiz.test:4007', '--tag', manifest.base_image]), /EDITORIAL_BUILD_BASE_OVERWRITE_FORBIDDEN/);
});
