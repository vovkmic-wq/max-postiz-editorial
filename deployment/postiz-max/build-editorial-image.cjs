const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

function buildArguments(args) {
  if (args.some((value, index) => value.startsWith('--') && !['--check', '--build', '--origin', '--tag'].includes(value)) ||
      args.includes('--check') && args.includes('--build')) throw new Error('EDITORIAL_BUILD_ARGUMENTS_INVALID');
  const value = name => { const index = args.indexOf(name); return index < 0 ? undefined : args[index + 1]; };
  const origin = value('--origin'), tag = value('--tag');
  if (!origin || !tag || !/^max-postiz-linux:[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}$/.test(tag)) throw new Error('EDITORIAL_BUILD_ARGUMENTS_INVALID');
  let url;
  try { url = new URL(origin); } catch { throw new Error('EDITORIAL_BUILD_ORIGIN_INVALID'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/' || origin !== url.origin) {
    throw new Error('EDITORIAL_BUILD_ORIGIN_INVALID');
  }
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'editorial-manifest.json')));
  if (tag === manifest.base_image) throw new Error('EDITORIAL_BUILD_BASE_OVERWRITE_FORBIDDEN');
  return { origin, tag, build: args.includes('--build'), manifest };
}
function main(args, run = spawnSync) {
  const options = buildArguments(args);
  // Проверка локального образа не читает env контейнера и не запускает сервисы.
  const inspected = run('docker', ['image', 'inspect', '--format', '{{.Id}}', options.manifest.base_image], { encoding: 'utf8' });
  if (inspected.status !== 0 || inspected.stdout.trim() !== options.manifest.base_image_id) throw new Error('EDITORIAL_BUILD_BASE_IMAGE_MISMATCH');
  if (!options.build) return 'Закреплённый базовый образ проверен. Сборка и запуск не выполнялись.';
  const built = run('docker', ['build', '--file', path.join(__dirname, 'Dockerfile.editorial'), '--build-arg', `PUBLIC_ORIGIN=${options.origin}`, '--tag', options.tag, __dirname], { stdio: 'inherit' });
  if (built.status !== 0) throw new Error('EDITORIAL_IMAGE_BUILD_FAILED');
  return `Образ ${options.tag} собран. Контейнеры не пересоздавались.`;
}
module.exports = { buildArguments, main };
if (require.main === module) {
  try { console.log(main(process.argv.slice(2))); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
