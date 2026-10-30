// Transpile the actual provider and exercise only mocked bridge requests, never MAX.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const tooling = process.env.EDITORIAL_TOOLING || (fs.existsSync('/app/node_modules/typescript')
  ? '/app/node_modules' : path.resolve(__dirname, '../../data/postiz-editorial-tooling/node_modules'));
const ts = require(path.join(tooling, 'typescript'));
const filename = process.env.MAX_PROVIDER_COMPILED || 'max-browser.provider.ts';
const compiled = process.env.MAX_PROVIDER_COMPILED ? { outputText: fs.readFileSync(filename, 'utf8'), diagnostics: [] }
  : ts.transpileModule(fs.readFileSync(path.join(__dirname, filename), 'utf8'), {
  fileName: filename, reportDiagnostics: true,
  compilerOptions: { target: ts.ScriptTarget.ES2017, module: ts.ModuleKind.CommonJS },
});
assert.deepEqual((compiled.diagnostics || []).filter(d => d.category === ts.DiagnosticCategory.Error), []);

const BASE = 'http://127.0.0.1:8092/api/max-bridge';
const integration = { id: 'fixture-integration' };
const posts = [{ id: 'existing-post', message: 'Must never be sent by this provider' }];
const token = 'fixture-channel-token';
const response = (result, status = 200) => ({ ok: status >= 200 && status < 300, json: async () => result });
class TerminalFailure extends Error {
  constructor(details) { super(details.message); this.nonRetryable = details.nonRetryable; }
}
function provider(fetchImpl, env = {}) {
  const calls = [];
  const module = { exports: {} };
  const sandbox = {
    module, exports: module.exports, URL, AbortSignal,
    process: { env: { MAX_BRIDGE_URL: BASE, MAX_POSTIZ_BRIDGE_KEY: 'fixture-key', ...env } },
    fetch: async (url, options) => {
      const body = JSON.parse(options.body);
      calls.push({ url, options, body });
      assert.ok(['/submit', '/status', '/connect', '/exchange'].some(p => url === BASE + p));
      assert.equal(options.method, 'POST');
      assert.equal(options.headers['X-Max-Bridge-Key'], 'fixture-key');
      return fetchImpl(url, options, body);
    },
    require(name) {
      if (name.endsWith('/social.abstract')) return { SocialAbstract: class {} };
      if (name.endsWith('/make.secure.id')) return { makeSecureId: () => 'fixture-secure-id' };
      if (name === '@temporalio/activity') return { ApplicationFailure: { create: details => new TerminalFailure(details) } };
      throw new Error('Unexpected provider dependency: ' + name);
    },
  };
  vm.runInNewContext(compiled.outputText, sandbox, { filename });
  return { instance: new module.exports.MaxBrowserProvider(), request: module.exports.maxBridgeRequest, calls };
}
const submit = p => p.instance.postPending('fixture-channel', token, posts, integration);
const status = (p, pending = { postId: 'existing-post' }) => p.instance.checkPostStatus(token, pending, integration);
function assertOnlyRegistrationAndReads(calls) {
  for (const call of calls) {
    assert.ok([BASE + '/submit', BASE + '/status'].includes(call.url));
    assert.deepEqual(Object.keys(call.body).sort(), ['channel_token', 'integration_id', 'post_id']);
    assert.equal(call.body.post_id, 'existing-post');
    assert.equal(call.body.integration_id, integration.id);
    assert.equal(call.body.channel_token, token);
  }
}

for (const acceptedBeforeReplyLost of [false, true]) {
  test(`submit transport loss stays pending, accepted before loss=${acceptedBeforeReplyLost}`, async () => {
    let registered = false;
    const p = provider(async url => {
      if (url.endsWith('/submit')) {
        registered = acceptedBeforeReplyLost;
        throw new TypeError('fetch failed after an uncertain network outcome');
      }
      return response({ state: 'published', external_id: 'verified-max-id', permalink: 'https://max.ru/fixture-only' });
    });
    const result = await submit(p);
    assert.equal(p.calls.length, 1, 'no resubmission after transport failure');
    assert.equal(registered, acceptedBeforeReplyLost);
    assert.equal(result[0].status, 'pending');
    assert.equal(result[0].id, 'existing-post');
    assert.equal(result[0].postId, 'existing-post');
    assert.equal(result[0].pendingData.postId, 'existing-post');
    assert.equal(result[0].releaseURL, '');
    const completed = await status(p, result[0].pendingData);
    assert.equal(completed.status, 'completed');
    assert.equal(completed.postId, 'verified-max-id');
    assert.equal(completed.releaseURL, 'https://max.ru/fixture-only');
    assert.equal(p.calls.length, 2);
    assertOnlyRegistrationAndReads(p.calls);
  });
}

test('status transport loss preserves the exact pending data and resumes by status only', async () => {
  let available = false;
  const p = provider(async () => {
    if (!available) throw new Error('timeout after request may have reached the agent');
    return response({ state: 'published', external_id: 'verified-existing-id' });
  });
  const pending = { postId: 'existing-post', preservedMarker: 'original' };
  const result = await status(p, pending);
  assert.equal(result.status, 'pending');
  assert.equal(result.pendingData, pending);
  assert.equal(p.calls.length, 1);
  available = true;
  const completed = await status(p, pending);
  assert.equal(completed.status, 'completed');
  assert.equal(completed.postId, 'verified-existing-id');
  assert.equal(completed.releaseURL, '');
  assert.equal(p.calls.length, 2);
  assert.ok(p.calls.every(c => c.url.endsWith('/status')));
  assertOnlyRegistrationAndReads(p.calls);
});

for (const state of ['unknown', 'sending', 'pending', 'review_pending']) {
  test(`nonterminal bridge state ${state} stays pending`, async () => {
    const p = provider(async () => response({ state }));
    const pending = { postId: 'existing-post' };
    const result = await status(p, pending);
    assert.equal(result.status, 'pending');
    assert.equal(result.pendingData, pending);
    assert.equal(p.calls.length, 1);
    assertOnlyRegistrationAndReads(p.calls);
  });
}

for (const state of ['failed', 'cancelled']) {
  test(`terminal bridge state ${state} remains a nonretryable failure`, async () => {
    const p = provider(async () => response({ state, error: 'EXACT_FAILURE' }));
    await assert.rejects(status(p), error => error instanceof TerminalFailure
      && error.nonRetryable === true && error.message === 'MAX delivery stopped: EXACT_FAILURE');
    assert.equal(p.calls.length, 1);
  });
}

for (const operation of [submit, status]) {
  for (const code of [401, 403, 500, 503]) {
    test(`${operation.name}: HTTP ${code} is not hidden as transient connectivity`, async () => {
      const p = provider(async () => response({ code: 'MAX_BROWSER_AGENT_UNAVAILABLE' }, code));
      await assert.rejects(operation(p), error => error.message === 'MAX_BROWSER_AGENT_UNAVAILABLE'
        && error.name !== 'MaxBridgeTransportUnavailable');
      assert.equal(p.calls.length, 1);
    });
  }
  test(`${operation.name}: invalid JSON remains explicit failure`, async () => {
    const p = provider(async () => ({ ok: true, json: async () => { throw new SyntaxError('invalid JSON'); } }));
    await assert.rejects(operation(p), /invalid JSON/);
    assert.equal(p.calls.length, 1);
  });
  test(`${operation.name}: arbitrary same-message error is not a typed transport failure`, async () => {
    const p = provider(async () => ({ ok: true, json: async () => { throw new Error('MAX_BROWSER_AGENT_UNAVAILABLE'); } }));
    await assert.rejects(operation(p), error => error.message === 'MAX_BROWSER_AGENT_UNAVAILABLE'
      && error.name === 'Error');
    assert.equal(p.calls.length, 1);
  });
  for (const env of [{ MAX_POSTIZ_BRIDGE_KEY: '' }, { MAX_BRIDGE_URL: 'https://untrusted.example/bridge' }]) {
    test(`${operation.name}: invalid bridge configuration fails without fetch`, async () => {
      const p = provider(async () => { throw new Error('must not fetch'); }, env);
      await assert.rejects(operation(p), /MAX_BROWSER_BRIDGE_NOT_CONFIGURED/);
      assert.equal(p.calls.length, 0);
    });
  }
}

test('request serialization failure is not reclassified as connectivity', async () => {
  const p = provider(async () => { throw new Error('must not fetch'); });
  const circular = {}; circular.self = circular;
  await assert.rejects(p.request('/submit', circular), /circular/i);
  assert.equal(p.calls.length, 0);
});

test('connection authentication still exposes transport failure to its caller', async () => {
  const p = provider(async () => { throw new TypeError('fetch failed'); });
  await assert.rejects(p.instance.authenticate({ code: 'fixture-code', codeVerifier: 'fixture-verifier' }),
    error => error.name === 'MaxBridgeTransportUnavailable' && error.message === 'MAX_BROWSER_AGENT_UNAVAILABLE');
  assert.equal(p.calls.length, 1);
});

test('multi-post submit is rejected before any bridge call', async () => {
  const p = provider(async () => { throw new Error('must not fetch'); });
  await assert.rejects(p.instance.postPending('fixture-channel', token, [...posts, { id: 'other-post' }], integration),
    error => error instanceof TerminalFailure && error.nonRetryable === true);
  assert.equal(p.calls.length, 0);
});
