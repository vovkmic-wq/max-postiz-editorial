const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

function main(args, run = spawnSync) {
  if (args.length !== 1 || !['--check', '--build'].includes(args[0])) throw new Error('PROVIDER_BUILD_ARGUMENTS_INVALID');
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'provider-reliability-manifest.json')));
  const inspected = run('docker', ['image', 'inspect', '--format', '{{.Id}}', manifest.base_image], { encoding: 'utf8' });
  if (inspected.status !== 0 || inspected.stdout.trim() !== manifest.base_image_id) throw new Error('PROVIDER_BUILD_BASE_MISMATCH');
  if (args[0] === '--check') return 'Закреплённый образ проверен; сборка и запуск не выполнялись.';
  const built = run('docker', ['build', '--file', path.join(__dirname, 'Dockerfile.reliability'),
    '--tag', 'max-postiz-linux:0.10.8-reliability', __dirname], { stdio: 'inherit' });
  if (built.status !== 0) throw new Error('PROVIDER_BUILD_FAILED');
  return 'Образ 0.10.8-reliability собран; контейнеры не пересоздавались.';
}
module.exports = { main };
if (require.main === module) {
  try { console.log(main(process.argv.slice(2))); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
