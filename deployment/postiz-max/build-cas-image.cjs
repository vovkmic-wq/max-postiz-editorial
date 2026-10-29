const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

function main(args, run = spawnSync) {
  if (args.length !== 1 || !['--check', '--build'].includes(args[0])) throw new Error('CAS_BUILD_ARGUMENTS_INVALID');
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'cas-manifest.json')));
  if (manifest.contract !== 'max-browser-cas-draft-schedule-v1' ||
      !/^sha256:[a-f0-9]{64}$/.test(manifest.base_image_id) ||
      manifest.base_image !== 'max-postiz-linux:0.10.8-reliability' ||
      manifest.target_image !== 'max-postiz-linux:0.11.2-illustrated-cas') throw new Error('CAS_BUILD_MANIFEST_INVALID');
  const dockerfile = path.join(__dirname, 'Dockerfile.cas');
  if (!fs.readFileSync(dockerfile, 'utf8').replace(/\r\n/g, '\n').includes('\nFROM ' + manifest.base_image + '\n')) throw new Error('CAS_BUILD_DOCKERFILE_MISMATCH');
  const inspected = run('docker', ['image', 'inspect', '--format', '{{.Id}}', manifest.base_image], { encoding: 'utf8' });
  if (inspected.status !== 0 || inspected.stdout.trim() !== manifest.base_image_id) throw new Error('CAS_BUILD_BASE_MISMATCH');
  if (args[0] === '--check') return 'Закреплённый образ проверен; сборка и запуск не выполнялись.';
  const built = run('docker', ['build', '--file', dockerfile, '--tag', manifest.target_image, __dirname], { stdio: 'inherit' });
  if (built.status !== 0) throw new Error('CAS_BUILD_FAILED');
  return 'Образ illustrated-cas собран; контейнеры не пересоздавались.';
}
module.exports = { main };
if (require.main === module) {
  try { console.log(main(process.argv.slice(2))); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
