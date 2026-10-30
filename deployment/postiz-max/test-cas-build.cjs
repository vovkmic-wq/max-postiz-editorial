// Pure build-command simulation: no Docker process or runtime writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { main } = require('./build-cas-image.cjs');
const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'cas-manifest.json')));
let calls = [], image = manifest.base_image_id, inspectStatus = 0, buildStatus = 0;
const run = (command, args) => {
  assert.equal(command, 'docker'); calls.push(args);
  return args[0] === 'image' ? { status: inspectStatus, stdout: image + '\n' } : { status: buildStatus };
};
assert.match(main(['--check'], run), /сборка и запуск не выполнялись/);
assert.equal(calls.length, 1);
assert.deepEqual(calls[0], ['image', 'inspect', '--format', '{{.Id}}', manifest.base_image]);
calls = []; main(['--build'], run);
assert.equal(calls.length, 2);
assert.equal(calls[1][0], 'build');
assert.equal(calls[1][3], '--tag');
assert.equal(calls[1][4], manifest.target_image);
for (const change of [() => { image = 'sha256:' + '0'.repeat(64); }, () => { inspectStatus = 1; }]) {
  calls = []; image = manifest.base_image_id; inspectStatus = 0; change();
  assert.throws(() => main(['--build'], run), /CAS_BUILD_BASE_MISMATCH/);
  assert.equal(calls.length, 1);
}
calls = []; image = manifest.base_image_id; inspectStatus = 0; buildStatus = 1;
assert.throws(() => main(['--build'], run), /CAS_BUILD_FAILED/);
for (const args of [[], ['--unknown'], ['--build', '--check']]) {
  calls = []; assert.throws(() => main(args, run), /CAS_BUILD_ARGUMENTS_INVALID/);
  assert.equal(calls.length, 0);
}
console.log(JSON.stringify({ build_simulation_checks: 8, docker_calls: 0, runtime_mutations: 0 }));
