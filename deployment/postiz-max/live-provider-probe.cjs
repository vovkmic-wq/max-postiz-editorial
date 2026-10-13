// Exercise the deployed provider against the real agent using an existing draft.
// Tokens stay inside the container. This script cannot schedule or approve a post.
const assert = require('node:assert/strict');
require('/app/node_modules/reflect-metadata');
const { PrismaClient } = require('/app/node_modules/@prisma/client');
const { MaxBrowserProvider, maxBridgeRequest } = require('/app/apps/backend/dist/libraries/nestjs-libraries/src/integrations/social/max-browser.provider.js');
const prisma = new PrismaClient();
(async () => {
  const post = await prisma.post.findUnique({ where: { id: process.argv[2] }, include: { integration: true } });
  assert(post && post.state === 'DRAFT' && post.integration.providerIdentifier === 'max-browser');
  const integration = post.integration;
  const scope = { channel_token: integration.token, integration_id: integration.id, post_id: post.id };
  const initial = await maxBridgeRequest('/status', scope);
  assert.equal(initial.state, 'review_pending');
  assert.equal(initial.paused, true);
  const provider = new MaxBrowserProvider();
  const pending = await provider.postPending(integration.internalId, integration.token,
    [{ id: post.id, message: post.content }], integration);
  assert.equal(pending[0].status, 'pending');
  const status = await provider.checkPostStatus(integration.token, pending[0].pendingData, integration);
  assert.equal(status.status, 'pending');
  const final = await maxBridgeRequest('/status', scope);
  assert.equal(final.state, 'review_pending');
  assert.equal(final.paused, true);
  console.log(JSON.stringify({ live_provider_bridge: 'passed', state: final.state,
    target_paused: true, approved: false, external_posts: 0 }));
})().catch(() => { console.error('Live provider draft probe failed'); process.exitCode = 1; })
  .finally(() => prisma.$disconnect());
