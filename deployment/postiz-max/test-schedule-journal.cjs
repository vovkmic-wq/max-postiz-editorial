const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { main: build } = require('./build-schedule-journal.cjs');
const manifest = require('./schedule-journal-manifest.json');
const ts = require(path.join(process.env.EDITORIAL_TOOLING, 'typescript'));

function fixture(bridge, rows) {
  const source = fs.readFileSync(path.join(__dirname, 'max-schedule-journal.ts'), 'utf8');
  const compiled = ts.transpileModule(source, { fileName: 'max-schedule-journal.ts', reportDiagnostics: true,
    compilerOptions: { target: ts.ScriptTarget.ES2017, module: ts.ModuleKind.CommonJS } });
  assert.deepEqual(compiled.diagnostics, []);
  const calls = [], exports = {};
  class Rejected extends Error {}
  vm.runInNewContext(compiled.outputText, { exports, Buffer,
    process: { env: { MAX_POSTIZ_BRIDGE_KEY: 'fixture-key', EDITORIAL_OWNER_ORGANIZATION_ID: 'fixture-org' } },
    require: name => {
      if (name === 'node:crypto') return require(name);
      if (name === '@nestjs/common') return { ConflictException: Rejected,
        ForbiddenException: Rejected, ServiceUnavailableException: Rejected };
      if (name.endsWith('/max-browser.provider')) return { maxBridgeRequest: async (route, data) => {
        calls.push([route, data]);
        if (bridge instanceof Error) throw bridge;
        return bridge;
      } };
      throw new Error('Unexpected schedule journal dependency ' + name);
    },
  });
  const prisma = { $queryRawUnsafe: async (...args) => {
    calls.push(args);
    return rows;
  } };
  const stamp = Date.now(), hash = 'a'.repeat(64);
  const binding = { operation_id: 'b2dbeff6-9db1-4c92-b939-2e79c8ea66dd', post_id: 'post',
    group_id: 'group', integration_id: 'integration', record_id: 'record', variant_id: 'variant',
    slot_id: 'slot', expected_hash: hash, content_sha256: hash, image_json_sha256: hash,
    settings_sha256: hash, draft_due_ms: stamp, due_ms: stamp + 60000, deadline_ms: stamp + 15000 };
  return { exports, prisma, binding, calls, org: 'fixture-org', key: 'fixture-key' };
}

test('untrusted or late requests never reach the bridge or database', async () => {
  const { exports, prisma, binding, calls, org, key } = fixture();
  await assert.rejects(exports.scheduleCapabilities(prisma, 'other-org', key), /ACCESS_DENIED/);
  await assert.rejects(exports.scheduleReceipt(prisma, org, 'wrong-key', binding.operation_id), /ACCESS_DENIED/);
  await assert.rejects(exports.changeSchedule(prisma, org, key, binding.operation_id,
    { ...binding, deadline_ms: Date.now() - 1000 }, 'schedule'), /MAX_SCHEDULE_DEADLINE/);
  await assert.rejects(exports.changeSchedule(prisma, org, key, binding.operation_id,
    { ...binding, extra: 'not bound' }, 'retire'), /INPUT_INVALID/);
  assert.deepEqual(calls, []);
});

test('retirement requires the exact Hub commit and never starts Temporal', async () => {
  const denied = fixture(new Error('fixture-error'));
  await assert.rejects(denied.exports.changeSchedule(denied.prisma, denied.org, denied.key,
    denied.binding.operation_id, denied.binding, 'retire'), /BRIDGE_UNAVAILABLE/);
  assert.equal(denied.calls.length, 1);
  const { exports, prisma, binding, calls, org, key } = fixture({ retired: true },
    [{ result: { outcome: 'draft_restored' } }]);
  const result = await exports.changeSchedule(prisma, org, key, binding.operation_id,
    binding, 'retire', () => assert.fail('unexpected Temporal start'));
  assert.equal(result.outcome, 'draft_restored');
  assert.equal(result.operation, binding);
  assert.deepEqual(calls[0], ['/schedule/retire', binding]);
  assert.match(calls[1][0], /max_schedule_change/);
});

test('only a new atomic schedule starts the native workflow', async () => {
  const id = 'b2dbeff6-9db1-4c92-b939-2e79c8ea66dd';
  for (const newlyScheduled of [true, false]) {
    const { exports, prisma, binding, calls, org, key } = fixture({ owner: id },
      [{ result: { outcome: 'scheduled', new_schedule: newlyScheduled } }]);
    const starts = [];
    const result = await exports.changeSchedule(prisma, org, key, id, binding, 'schedule',
      async postId => starts.push(postId));
    assert.equal(result.outcome, 'scheduled');
    assert.deepEqual(calls[0], ['/schedule/start', binding]);
    assert.deepEqual(starts, newlyScheduled ? [binding.post_id] : []);
  }
});

test('build inherits the exact working image and leaves a foreign alias alone', () => {
  const calls = [];
  assert.throws(() => build(['--build'], () => ({ status: 0, stdout: 'different' })), /BASE_MISMATCH/);
  const run = (alias) => (_executable, args) => {
    calls.push(args);
    if (args[1] === 'inspect' && args.at(-1) === manifest.base_image) {
      return alias === null ? { status: 1, stdout: '' } : { status: 0, stdout: alias };
    }
    return { status: 0, stdout: manifest.base_image_id };
  };
  build(['--check'], run(null));
  assert.deepEqual(calls.map(args => args.at(-1)), [manifest.base_image_id, manifest.base_image]);
  calls.length = 0;
  assert.throws(() => build(['--build'], run('sha256:foreign')), /BASE_MISMATCH/);
  assert.equal(calls.length, 2);
  calls.length = 0;
  build(['--build'], run(null));
  assert.deepEqual(calls[2], ['image', 'tag', manifest.base_image_id, manifest.base_image]);
  assert.equal(calls[3][calls[3].indexOf('--tag') + 1], manifest.target_image);
  assert.equal(calls[3][calls[3].indexOf('--network') + 1], 'none');
});
