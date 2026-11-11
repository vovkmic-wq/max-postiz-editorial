const path = require('node:path');
const { spawnSync } = require('node:child_process');
const manifest = require('./schedule-journal-manifest.json');

function main(args, run = spawnSync) {
  if (args.length !== 1 || !['--check', '--build'].includes(args[0])) {
    throw new Error('SCHEDULE_BUILD_ARGUMENTS_INVALID');
  }
  const inspect = reference => run('docker', ['image', 'inspect', '--format', '{{.Id}}', reference],
    { encoding: 'utf8' });
  const base = inspect(manifest.base_image_id);
  const alias = inspect(manifest.base_image);
  if (base.status !== 0 || base.stdout?.trim() !== manifest.base_image_id ||
      (alias.status === 0 && alias.stdout?.trim() !== manifest.base_image_id)) {
    throw new Error('SCHEDULE_BUILD_BASE_MISMATCH');
  }
  if (args[0] === '--check') return 'Базовый образ журнала Postiz проверен.';
  if (alias.status !== 0) {
    const tagged = run('docker', ['image', 'tag', manifest.base_image_id, manifest.base_image], { stdio: 'inherit' });
    if (tagged.status !== 0) throw new Error('SCHEDULE_BUILD_BASE_TAG_FAILED');
  }
  const built = run('docker', ['build', '--network', 'none', '--pull=false',
    '--file', path.join(__dirname, 'Dockerfile.schedule-journal'),
    '--tag', manifest.target_image, __dirname], { stdio: 'inherit' });
  if (built.status !== 0) throw new Error('SCHEDULE_BUILD_FAILED');
  return 'Образ Postiz с журналом собран.';
}

module.exports = { main };
if (require.main === module) {
  try { console.log(main(process.argv.slice(2))); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
