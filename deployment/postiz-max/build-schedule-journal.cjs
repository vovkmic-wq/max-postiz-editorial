const path = require('node:path');
const { spawnSync } = require('node:child_process');
const manifest = require('./schedule-journal-manifest.json');

function main(args, run = spawnSync) {
  if (args.length !== 1 || !['--check', '--build'].includes(args[0])) {
    throw new Error('SCHEDULE_BUILD_ARGUMENTS_INVALID');
  }
  const base = run('docker', ['image', 'inspect', '--format', '{{.Id}}', manifest.base_image], { encoding: 'utf8' });
  if (base.status !== 0 || base.stdout.trim() !== manifest.base_image_id) {
    throw new Error('SCHEDULE_BUILD_BASE_MISMATCH');
  }
  if (args[0] === '--check') return 'Базовый образ журнала Postiz проверен.';
  const built = run('docker', ['build', '--file', path.join(__dirname, 'Dockerfile.schedule-journal'),
    '--tag', manifest.target_image, __dirname], { stdio: 'inherit' });
  if (built.status !== 0) throw new Error('SCHEDULE_BUILD_FAILED');
  return 'Образ Postiz с журналом собран.';
}

module.exports = { main };
if (require.main === module) {
  try { console.log(main(process.argv.slice(2))); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
