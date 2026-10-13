const assert = require('node:assert/strict');
require('/app/node_modules/reflect-metadata');
const { MaxBrowserProvider } = require('/app/apps/backend/dist/libraries/nestjs-libraries/src/integrations/social/max-browser.provider.js');
process.env.MAX_POSTIZ_BRIDGE_KEY = 'contract-fixture-key';
process.env.MAX_BRIDGE_URL = 'http://localhost:8090/api/max-bridge';
const calls = [];
let state = 'review_pending';
global.fetch = async (url, options) => {
  const data = JSON.parse(options.body); calls.push({ url, data });
  assert.equal(options.headers['X-Max-Bridge-Key'], 'contract-fixture-key');
  const result = url.endsWith('/connect') ? { code: 'fixture-code' }
    : url.endsWith('/exchange') ? { id: '-fixture', accessToken: 'fixture-channel-token' }
    : { state, external_id: 'fixture-mid', permalink: 'https://example.org/fixture-only' };
  return new Response(JSON.stringify(result), { status: 200 });
};
(async () => {
  const provider = new MaxBrowserProvider();
  assert.equal(provider.identifier, 'max-browser');
  const auth = await provider.generateAuthUrl();
  assert.equal(new URL(auth.url).pathname, '/integrations/social/max-browser');
  assert.equal(new URL(auth.url).searchParams.get('code'), 'fixture-code');
  assert.equal((await provider.authenticate({ code: 'fixture-code', codeVerifier: auth.codeVerifier })).id, '-fixture');
  const integration = { id: 'fixture-integration' };
  const pending = await provider.postPending('-fixture', 'fixture-channel-token', [{ id: 'fixture-post', message: 'Test' }], integration);
  assert.equal(pending[0].status, 'pending');
  assert.equal((await provider.checkPostStatus('fixture-channel-token', pending[0].pendingData, integration)).status, 'pending');
  state = 'published';
  assert.equal((await provider.checkPostStatus('fixture-channel-token', pending[0].pendingData, integration)).postId, 'fixture-mid');
  state = 'unknown';
  assert.equal((await provider.checkPostStatus('fixture-channel-token', pending[0].pendingData, integration)).status, 'pending');
  assert.notEqual(await provider.checkValidity([[{ path: 'one' }, { path: 'two' }]]), true);
  assert(calls.every(c => c.url.startsWith('http://localhost:8090/api/max-bridge')));
  console.log(JSON.stringify({ provider_contract: 'passed', calls: calls.length, max_network_calls: 0 }));
})().catch(() => { console.error('Provider contract failed'); process.exit(1); });
