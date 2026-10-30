// Exercise the actual compiled guard with in-memory dependencies, never a live bridge.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const filename = process.env.MAX_CAS_COMPILED || '/app/apps/backend/dist/apps/backend/src/max-browser.guard.js';
const source = fs.readFileSync(filename, 'utf8');
const HASH = 'a'.repeat(64), calls = [];
const IMAGE = { id: 'image-fixture', path: '/uploads/exact.jpg' };
const SETTINGS = { __type: 'max-browser', channel: 'fixture-channel' };
const storedPost = (images = []) => ({ id: 'existing-post', parentPostId: null,
  group: 'old-group', integrationId: 'integration-max', state: 'DRAFT', content: 'Exact caption',
  image: JSON.stringify(images), settings: JSON.stringify(SETTINGS) });
let failure = null, leaseOverride = null, posts = [storedPost()];
const exportsValue = {};
class ConflictException extends Error {}
const sandbox = { exports: exportsValue, process: { env: { MAX_POSTIZ_BRIDGE_KEY: 'fixture-key' } },
  require: name => {
    if (name === 'tslib') return require(process.env.MAX_CAS_TSLIB || '/app/node_modules/tslib');
    if (name === '@nestjs/common') return { ConflictException, Injectable: () => value => value };
    if (name.endsWith('/prisma.service')) return { PrismaService: class {} };
    if (name.endsWith('/max-browser.provider')) return { maxBridgeRequest: async (path, payload) => {
      calls.push({ path, payload });
      if (failure) throw new Error(failure);
      return { owner: 'fixture-owner', cas_verified: true,
        expected_hash: payload.expected_hash, expected_post_id: payload.expected_post_id, ...leaseOverride };
    } };
    throw new Error('Unexpected guard dependency: ' + name);
  } };
vm.runInNewContext(source, sandbox, { filename });
const Guard = exportsValue.MaxBrowserMutationGuard;
const guard = new Guard({ post: { findMany: async () => posts } });
function body(type = 'draft') {
  return { type, republish: false, date: new Date(Date.now() + 600000).toISOString(),
    shortLink: false, tags: [], posts: [{ group: 'old-group', integration: { id: 'integration-max' },
      settings: { ...SETTINGS }, value: [{ id: 'existing-post', content: 'Exact caption',
        image: type === 'schedule' ? [{ ...IMAGE }] : [] }] }] };
}
function context(header = HASH, value = null, method = 'POST', path = '/public/v1/posts') {
  const headers = {}, listeners = {};
  const req = { method, path, headers: header === null ? {} : { 'x-max-postiz-expected-hash': header },
    body: value || body() };
  const res = { once: (event, callback) => { listeners[event] = callback; },
    setHeader: (name, value) => { headers[name.toLowerCase()] = value; } };
  return { req, headers, listeners, switchToHttp: () => ({ getRequest: () => req, getResponse: () => res }) };
}
(async () => {
  let checks = 0;
  function reset(type = 'draft') {
    calls.length = 0; failure = null; leaseOverride = null;
    posts = [storedPost(type === 'schedule' ? [IMAGE] : [])];
  }
  async function success(value, path = '/public/v1/posts') {
    const exact = context(HASH, value, 'POST', path);
    assert.equal(await guard.canActivate(exact), true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].path, '/mutation/start');
    assert.equal(calls[0].payload.expected_hash, HASH);
    assert.equal(calls[0].payload.expected_post_id, 'existing-post');
    assert.deepEqual(Array.from(calls[0].payload.identifiers), ['existing-post']);
    assert.equal(exact.headers['x-max-postiz-cas-verified'], HASH);
    assert.equal(typeof exact.listeners.finish, 'function');
    exact.listeners.finish();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(calls.length, 2);
    assert.equal(calls[1].path, '/mutation/finish');
    assert.equal(calls[1].payload.owner, 'fixture-owner');
    checks++;
  }
  reset(); await success(body());
  reset('schedule'); await success(body('schedule'));
  reset('schedule');
  const microseconds = body('schedule');
  microseconds.date = microseconds.date.replace(/Z$/, '000+00:00');
  microseconds.posts[0].value[0].image = [{ path: IMAGE.path, id: IMAGE.id }];
  microseconds.posts[0].settings = { channel: SETTINGS.channel, __type: 'max-browser' };
  await success(microseconds, '/posts');
  reset('schedule');
  const realistic = body('schedule');
  realistic.date = realistic.date.replace(/Z$/, '456+00:00');
  realistic.posts[0].settings = { hub_variant_id: 'fixture-variant',
    hub_correlation: 'fixture-correlation', __type: 'max-browser' };
  realistic.posts[0].value[0].image = [{ id: '00000000-0000-4000-8000-000000000001',
    path: 'http://127.0.0.1:4007/uploads/2026/10/29/0123456789abcdef0123456789abcdef.jpg' }];
  posts[0].settings = JSON.stringify(realistic.posts[0].settings);
  posts[0].image = JSON.stringify(realistic.posts[0].value[0].image);
  await success(realistic);

  for (const type of ['draft', 'schedule']) for (const code of
      ['BRIDGE_REMOTE_CHANGED', 'BRIDGE_EDIT_IN_PROGRESS', 'BRIDGE_DELIVERY_LOCKED']) {
    reset(type); failure = code;
    const conflict = context(HASH, body(type));
    let controllerCalls = 0;
    await assert.rejects(async () => { await guard.canActivate(conflict); controllerCalls++; }, new RegExp(code));
    assert.equal(controllerCalls, 0);
    assert.equal(conflict.headers['x-max-postiz-cas-verified'], undefined);
    assert.equal(conflict.listeners.finish, undefined);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].path, '/mutation/start');
    checks++;
  }

  for (const header of ['invalid', ['a'.repeat(64)], 'A'.repeat(64)]) {
    reset();
    await assert.rejects(guard.canActivate(context(header)));
    assert.equal(calls.length, 0);
    checks++;
  }
  for (const [method, path] of [['PUT', '/public/v1/posts'], ['POST', '/public/v1/posts/existing-post'],
    ['DELETE', '/posts/existing-post'], ['POST', '/wrong/posts']]) {
    reset();
    await assert.rejects(guard.canActivate(context(HASH, body(), method, path)));
    assert.equal(calls.length, 0); checks++;
  }
  const malformed = [
    value => { value.type = 'now'; }, value => { value.type = 'update'; },
    value => { value.type = 'unknown'; }, value => { value.republish = true; },
    value => { value.posts.push(value.posts[0]); },
    value => { value.posts[0].value.push({ id: 'other-post' }); },
    value => { delete value.posts[0].value[0].id; },
    value => { value.posts[0].group = 'other-group'; },
    value => { delete value.posts[0].group; },
    value => { value.posts[0].integration.id = 'other-integration'; },
    value => { delete value.posts[0].integration; },
  ];
  for (const type of ['draft', 'schedule']) for (const change of malformed) {
    reset(type); const value = body(type); change(value);
    await assert.rejects(guard.canActivate(context(HASH, value)));
    assert.equal(calls.length, 0);
    checks++;
  }
  const badSchedule = [
    value => { delete value.republish; }, value => { value.shortLink = true; },
    value => { value.tags = [{ value: 'new-tag', label: 'new-tag' }]; },
    value => { value.inter = 1; }, value => { value.posts[0].date = value.date; },
    value => { value.posts[0].value[0].delay = 60; },
    value => { delete value.date; }, value => { value.date = 'invalid'; },
    value => { value.date = '2099'; }, value => { value.date = Date.now() + 600000; },
    value => { value.date = new Date(Date.now() - 60000).toISOString(); },
    value => { value.date = '2099-02-30T12:00:00Z'; },
    value => { value.date = '2099-01-01T12:00:00+03:00'; },
    value => { value.posts[0].value[0].image = []; },
    value => { value.posts[0].value[0].image.push({ ...IMAGE }); },
    value => { value.posts[0].value[0].image = [null]; },
    value => { delete value.posts[0].value[0].image[0].id; },
    value => { delete value.posts[0].value[0].image[0].path; },
    value => { value.posts[0].value[0].image[0].id = 'other-image'; },
    value => { value.posts[0].value[0].image[0].path = '/uploads/other.jpg'; },
    value => { value.posts[0].value[0].image[0].path += '\n'; },
    value => { value.posts[0].value[0].image[0].extra = 'unbound'; },
    value => { value.posts[0].value[0].image[0].alt = 42; },
    value => { value.posts[0].value[0].image[0].thumbnail = {}; },
    value => { value.posts[0].value[0].content = 'Changed caption'; },
    value => { value.posts[0].settings.channel = 'other-channel'; },
  ];
  for (const change of badSchedule) {
    reset('schedule'); const value = body('schedule'); change(value);
    const invalid = context(HASH, value);
    await assert.rejects(guard.canActivate(invalid), /MAX_BROWSER_CAS_SCHEDULE_/);
    assert.equal(calls.length, 0);
    assert.equal(invalid.headers['x-max-postiz-cas-verified'], undefined);
    checks++;
  }
  for (const state of ['QUEUE', 'PUBLISHED', 'ERROR', 'unknown']) {
    reset('schedule'); posts[0].state = state;
    await assert.rejects(guard.canActivate(context(HASH, body('schedule'))));
    assert.equal(calls.length, 0); checks++;
  }
  for (const column of ['image', 'settings']) {
    reset('schedule'); posts[0][column] = 'invalid JSON';
    await assert.rejects(guard.canActivate(context(HASH, body('schedule'))), /MAX_BROWSER_CAS_SCHEDULE_CHANGED/);
    assert.equal(calls.length, 0); checks++;
  }
  for (const change of [() => { posts = []; }, () => { posts.push(storedPost()); },
    () => { posts[0].parentPostId = 'another-post'; }]) {
    reset('schedule'); change();
    await assert.rejects(guard.canActivate(context(HASH, body('schedule'))));
    assert.equal(calls.length, 0); checks++;
  }
  for (const data of [{ owner: '' }, { owner: false }, { owner: 'x'.repeat(37) }, { cas_verified: false },
    { expected_hash: 'b'.repeat(64) }, { expected_post_id: 'other-post' }]) {
    reset('schedule'); leaseOverride = data;
    const invalid = context(HASH, body('schedule'));
    await assert.rejects(guard.canActivate(invalid), /MAX_BROWSER_CAS_UNCONFIRMED/);
    assert.equal(invalid.headers['x-max-postiz-cas-verified'], undefined);
    assert.equal(invalid.listeners.finish, undefined);
    assert.equal(calls.filter(row => row.path === '/mutation/start').length, 1);
    const owner = Object.hasOwn(data, 'owner') ? data.owner : 'fixture-owner';
    assert.equal(calls.length, typeof owner === 'string' && owner && owner.length <= 36 ? 2 : 1);
    checks++;
  }
  reset();
  const legacy = context(null);
  assert.equal(await guard.canActivate(legacy), true);
  assert.equal(calls[0].payload.expected_hash, undefined);
  assert.equal(legacy.headers['x-max-postiz-cas-verified'], undefined);
  checks++;
  console.log(JSON.stringify({ compiled_guard_checks: checks, live_bridge_calls: 0,
    contract: 'max-browser-cas-draft-schedule-v1' }));
})().catch(error => { console.error(error); process.exitCode = 1; });
