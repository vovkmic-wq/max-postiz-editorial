// Exercise the actual compiled guard with in-memory dependencies, never a live bridge.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const filename = '/app/apps/backend/dist/apps/backend/src/max-browser.guard.js';
const source = fs.readFileSync(filename, 'utf8');
const HASH = 'a'.repeat(64), calls = [];
let fail = false, posts = [{ id: 'existing-post', parentPostId: null }];
const exportsValue = {};
class ConflictException extends Error {}
const sandbox = { exports: exportsValue, process: { env: { MAX_POSTIZ_BRIDGE_KEY: 'fixture-key' } },
  require: name => {
    if (name === 'tslib') return require('/app/node_modules/tslib');
    if (name === '@nestjs/common') return { ConflictException, Injectable: () => value => value };
    if (name.endsWith('/prisma.service')) return { PrismaService: class {} };
    if (name.endsWith('/max-browser.provider')) return { maxBridgeRequest: async (path, payload) => {
      calls.push({ path, payload });
      if (fail) throw new Error('BRIDGE_REMOTE_CHANGED');
      return { owner: 'fixture-owner', cas_verified: true,
        expected_hash: payload.expected_hash, expected_post_id: payload.expected_post_id };
    } };
    throw new Error('Unexpected guard dependency: ' + name);
  } };
vm.runInNewContext(source, sandbox, { filename });
const Guard = exportsValue.MaxBrowserMutationGuard;
const guard = new Guard({ post: { findMany: async () => posts } });
function context(header = HASH, body = null, method = 'POST', path = '/public/v1/posts') {
  const headers = {}, listeners = {};
  const req = { method, path, headers: header === null ? {} : { 'x-max-postiz-expected-hash': header },
    body: body || { type: 'draft', posts: [{ group: 'old-group', value: [{ id: 'existing-post' }] }] } };
  const res = { once: (event, callback) => { listeners[event] = callback; },
    setHeader: (name, value) => { headers[name.toLowerCase()] = value; } };
  return { req, headers, listeners, switchToHttp: () => ({ getRequest: () => req, getResponse: () => res }) };
}
(async () => {
  let checks = 0;
  const exact = context();
  assert.equal(await guard.canActivate(exact), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/mutation/start');
  assert.equal(calls[0].payload.expected_hash, HASH);
  assert.equal(calls[0].payload.expected_post_id, 'existing-post');
  assert.equal(exact.headers['x-max-postiz-cas-verified'], HASH);
  assert.equal(typeof exact.listeners.finish, 'function');
  exact.listeners.finish();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls[1].path, '/mutation/finish');
  assert.equal(calls[1].payload.owner, 'fixture-owner');
  checks++;

  calls.length = 0; fail = true;
  const conflict = context();
  await assert.rejects(guard.canActivate(conflict), /BRIDGE_REMOTE_CHANGED/);
  assert.equal(conflict.headers['x-max-postiz-cas-verified'], undefined);
  assert.equal(conflict.listeners.finish, undefined);
  assert.equal(calls.length, 1);
  checks++; fail = false;

  for (const header of ['invalid', ['a'.repeat(64)], 'A'.repeat(64)]) {
    calls.length = 0;
    await assert.rejects(guard.canActivate(context(header)));
    assert.equal(calls.length, 0);
    checks++;
  }
  for (const body of [
    { type: 'draft', posts: [{ value: [{ id: 'existing-post' }, { id: 'other-post' }] }] },
    { type: 'draft', posts: [{ value: [{ content: 'new post' }] }] },
    { type: 'draft', posts: [{ value: [{ id: 'existing-post' }] }, { value: [{ id: 'other-post' }] }] },
  ]) {
    calls.length = 0;
    await assert.rejects(guard.canActivate(context(HASH, body)));
    assert.equal(calls.length, 0);
    checks++;
  }
  calls.length = 0; posts = [];
  await assert.rejects(guard.canActivate(context()));
  assert.equal(calls.length, 0);
  checks++; posts = [{ id: 'existing-post', parentPostId: null }];

  calls.length = 0;
  const legacy = context(null);
  assert.equal(await guard.canActivate(legacy), true);
  assert.equal(calls[0].payload.expected_hash, undefined);
  assert.equal(legacy.headers['x-max-postiz-cas-verified'], undefined);
  checks++;
  console.log(JSON.stringify({ compiled_guard_checks: checks, live_bridge_calls: 0 }));
})().catch(error => { console.error(error); process.exitCode = 1; });
