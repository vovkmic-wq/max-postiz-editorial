// Сборка проверяется на временных файловых fixtures и mocked Docker, без контейнеров.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const tooling = process.env.EDITORIAL_TOOLING || (fs.existsSync('/app/node_modules/typescript')
  ? '/app/node_modules' : path.resolve(__dirname, '../../data/postiz-editorial-tooling/node_modules'));
const ts = require(path.join(tooling, 'typescript'));
const { planPatch, applyPatch } = require('./patch-provider-reliability.cjs');
const { main } = require('./build-reliability-image.cjs');
const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'provider-reliability-manifest.json')));
const hash = value => crypto.createHash('sha256').update(value).digest('hex');

function fixture() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'postiz-provider-reliability-'));
  const root = path.join(directory, 'image'), adapter = path.join(directory, 'adapter');
  fs.mkdirSync(adapter, { recursive: true });
  const local = JSON.parse(JSON.stringify(manifest));
  const files = { [local.source]: 'fixture old provider source', 'unrelated-cas-guard.js': 'must stay exact' };
  for (const name of Object.keys(local.compiled)) files[name] = 'fixture old compiled provider';
  local.source_sha256 = hash(files[local.source]);
  local.compiled = Object.fromEntries(Object.keys(local.compiled).map(name => [name, hash(files[name])]));
  for (const [name, content] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(root, name)), { recursive: true });
    fs.writeFileSync(path.join(root, name), content);
  }
  fs.writeFileSync(path.join(adapter, 'provider-reliability-manifest.json'), JSON.stringify(local));
  fs.copyFileSync(path.join(__dirname, 'max-browser.provider.ts'), path.join(adapter, 'max-browser.provider.ts'));
  return { directory, root, adapter, manifest: local };
}
function snapshot(directory, prefix = '') {
  const files = {};
  for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
    const relative = path.join(prefix, item.name), filename = path.join(directory, item.name);
    if (item.isDirectory()) Object.assign(files, snapshot(filename, relative));
    else files[relative] = hash(fs.readFileSync(filename));
  }
  return files;
}
function cleanup(f) {
  assert.ok(f.directory.startsWith(path.join(os.tmpdir(), 'postiz-provider-reliability-')));
  fs.rmSync(f.directory, { recursive: true, force: true });
}

test('plan and apply alter exactly provider source and the two compiled copies', () => {
  const f = fixture();
  try {
    const before = snapshot(f.root);
    const plan = planPatch(f.root, f.adapter, ts);
    assert.deepEqual(snapshot(f.root), before, 'planning performs no writes');
    const expected = [f.manifest.source, ...Object.keys(f.manifest.compiled)];
    assert.deepEqual(plan.map(([name]) => name), expected);
    assert.equal(plan.length, 3);
    assert.equal(plan[0][1], fs.readFileSync(path.join(f.adapter, 'max-browser.provider.ts'), 'utf8'));
    assert.equal(plan[1][1], plan[2][1]);
    assert.ok(plan[1][1].includes('require("../social.abstract")'));
    assert.ok(plan[1][1].includes('require("../../services/make.secure.id")'));
    assert.equal(plan[1][1].includes('@gitroom/'), false);
    const receipt = applyPatch(f.root, f.adapter, ts);
    assert.deepEqual(Object.keys(receipt), expected);
    const after = snapshot(f.root);
    assert.deepEqual(Object.keys(after).sort(), Object.keys(before).sort(), 'no other files created');
    const changed = Object.keys(after).filter(name => after[name] !== before[name]).sort();
    assert.deepEqual(changed, expected.map(name => path.normalize(name)).sort());
    for (const [relative, content] of plan) {
      assert.equal(fs.readFileSync(path.join(f.root, relative), 'utf8'), content);
      assert.equal(receipt[relative], hash(content));
    }
    assert.equal(fs.readFileSync(path.join(f.root, 'unrelated-cas-guard.js'), 'utf8'), 'must stay exact');
  } finally { cleanup(f); }
});

for (const relative of [manifest.source, ...Object.keys(manifest.compiled)]) {
  test(`base mismatch ${relative} rejects before any write`, () => {
    const f = fixture();
    try {
      fs.writeFileSync(path.join(f.root, relative), 'unexpected image content');
      const before = snapshot(f.root);
      assert.throws(() => applyPatch(f.root, f.adapter, ts), /PROVIDER_BASE_MISMATCH/);
      assert.deepEqual(snapshot(f.root), before);
    } finally { cleanup(f); }
  });
}

test('compiler version mismatch rejects before any source or compiled writes', () => {
  const f = fixture();
  try {
    const before = snapshot(f.root);
    assert.throws(() => applyPatch(f.root, f.adapter, { ...ts, version: '0.0.0-untrusted' }),
      /PROVIDER_COMPILER_VERSION_MISMATCH/);
    assert.deepEqual(snapshot(f.root), before);
  } finally { cleanup(f); }
});

for (const [name, source, error] of [
  ['invalid TypeScript', 'export class Broken { missing(', /PROVIDER_TYPESCRIPT_INVALID/],
  ['unresolved alias', "import { value } from '@gitroom/unhandled/provider'; export const keep = value;", /PROVIDER_UNRESOLVED_ALIAS/],
]) {
  test(`${name} rejects before any source or compiled writes`, () => {
    const f = fixture();
    try {
      fs.writeFileSync(path.join(f.adapter, 'max-browser.provider.ts'), source);
      const before = snapshot(f.root);
      assert.throws(() => applyPatch(f.root, f.adapter, ts), error);
      assert.deepEqual(snapshot(f.root), before);
    } finally { cleanup(f); }
  });
}

for (const inspected of [{ status: 0, stdout: 'sha256:wrong-image' }, { status: 1, stdout: '' }]) {
  test(`image pin failure status=${inspected.status} prevents build`, () => {
    const calls = [];
    assert.throws(() => main(['--build'], (binary, args) => { calls.push([binary, args]); return inspected; }),
      /PROVIDER_BUILD_BASE_MISMATCH/);
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], 'docker');
    assert.deepEqual(calls[0][1], ['image', 'inspect', '--format', '{{.Id}}', manifest.base_image]);
  });
}

test('check inspects only the exact pinned image, build adds no runtime Docker operations', () => {
  const checkCalls = [], buildCalls = [];
  const fake = calls => (binary, args, options) => {
    calls.push({ binary, args, options });
    return { status: 0, stdout: manifest.base_image_id + '\n' };
  };
  main(['--check'], fake(checkCalls));
  assert.equal(checkCalls.length, 1);
  main(['--build'], fake(buildCalls));
  assert.equal(buildCalls.length, 2);
  assert.deepEqual(buildCalls[0].args, checkCalls[0].args);
  assert.deepEqual(buildCalls[1].args, ['build', '--file', path.join(__dirname, 'Dockerfile.reliability'),
    '--tag', 'max-postiz-linux:0.10.8-reliability', __dirname]);
  assert.notEqual('max-postiz-linux:0.10.8-reliability', manifest.base_image, 'base tag cannot be overwritten');
  assert.ok([...checkCalls, ...buildCalls].every(c => c.binary === 'docker'));
  const prohibited = new Set(['run', 'up', 'start', 'stop', 'restart', 'rm', 'exec', 'compose', 'pull', 'push']);
  assert.equal([...checkCalls, ...buildCalls].flatMap(c => c.args).some(value => prohibited.has(value)), false);
});

test('failed image build stays explicit and performs no container operation', () => {
  const calls = [];
  assert.throws(() => main(['--build'], (binary, args) => {
    calls.push(args);
    return args[0] === 'image' ? { status: 0, stdout: manifest.base_image_id } : { status: 1 };
  }), /PROVIDER_BUILD_FAILED/);
  assert.equal(calls.length, 2);
  assert.equal(calls[1][0], 'build');
});

for (const args of [[], ['--unknown'], ['--check', '--build']]) {
  test(`invalid build arguments ${JSON.stringify(args)} perform no Docker call`, () => {
    let calls = 0;
    assert.throws(() => main(args, () => { calls++; }), /PROVIDER_BUILD_ARGUMENTS_INVALID/);
    assert.equal(calls, 0);
  });
}

test('Dockerfile uses the manifest base and tests both actual compiled providers', () => {
  const dockerfile = fs.readFileSync(path.join(__dirname, 'Dockerfile.reliability'), 'utf8');
  const from = [...dockerfile.matchAll(/^FROM\s+(\S+)\s*$/gm)];
  assert.equal(from.length, 1);
  assert.equal(from[0][1], manifest.base_image);
  assert.match(manifest.base_image_id, /^sha256:[a-f0-9]{64}$/);
  assert.equal(manifest.typescript_version, ts.version);
  assert.equal(Object.keys(manifest.compiled).length, 2);
  assert.equal(new Set(Object.values(manifest.compiled)).size, 1);
  for (const relative of Object.keys(manifest.compiled)) {
    assert.ok(dockerfile.includes(`MAX_PROVIDER_COMPILED=/app/${relative} node /adapter-reliability/test-provider-unavailable.cjs`));
  }
  assert.equal(dockerfile.includes('frontend'), false, 'no frontend rebuild');
});
