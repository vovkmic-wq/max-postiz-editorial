const fs = require('node:fs');
const crypto = require('node:crypto');
const path = require('node:path');
const root = '/app';
const manifest = JSON.parse(fs.readFileSync('/adapter/source-manifest.json', 'utf8'));
function edit(name, transform) {
  const file = path.join(root, name), before = fs.readFileSync(file, 'utf8');
  if (crypto.createHash('sha256').update(before).digest('hex') !== manifest.sha256[name]) {
    throw new Error('Unsupported upstream source: ' + name);
  }
  const after = transform(before);
  if (after === before) throw new Error('Patch did not change ' + name);
  fs.writeFileSync(file, after);
}
function once(text, needle, value) {
  if (text.split(needle).length !== 2) throw new Error('Ambiguous patch anchor');
  return text.replace(needle, value);
}
const manager = 'libraries/nestjs-libraries/src/integrations/integration.manager.ts';
edit('libraries/nestjs-libraries/src/temporal/temporal.module.ts', t => once(t,
  ".filter((f) => f.identifier.indexOf('-') === -1)",
  ".filter((f) => f.identifier.indexOf('-') === -1 || f.identifier === 'max-browser')"));
edit(manager, t => "import { MaxBrowserProvider } from '@gitroom/nestjs-libraries/integrations/social/max-browser.provider';\n" +
  once(t, '  new TelegramProvider(),', '  new TelegramProvider(),\n  new MaxBrowserProvider(),'));
edit('libraries/nestjs-libraries/src/dtos/posts/providers-settings/all.providers.settings.ts', t =>
  once(once(t, "  | ProviderExtension<'telegram', None>", "  | ProviderExtension<'telegram', None>\n  | ProviderExtension<'max-browser', None>"),
    "    { value: setEmpty, name: 'telegram' },", "    { value: setEmpty, name: 'telegram' },\n    { value: setEmpty, name: 'max-browser' },"));
edit('apps/backend/src/app.module.ts', t => "import { MaxBrowserMutationGuard } from './max-browser.guard';\n" +
  once(t, '    FILTER,', '    FILTER,\n    { provide: APP_GUARD, useClass: MaxBrowserMutationGuard },'));
edit('apps/frontend/src/components/new-launch/providers/show.all.providers.tsx', t => once(t,
  "'use client';", "'use client';\nimport MaxBrowserProvider from './max-browser/max-browser.provider';")
  .replace('export const Providers = [', "export const Providers = [\n  { identifier: 'max-browser', component: MaxBrowserProvider },"));
// The agent must see media and thread membership before approving or sending.
edit('libraries/nestjs-libraries/src/database/prisma/posts/posts.repository.ts', t => {
  const start = t.indexOf('  async getPosts(orgId:');
  const end = t.indexOf('  async getPostsList(', start);
  if (start < 0 || end < 0) throw new Error('Post projection missing');
  const region = t.slice(start, end);
  return t.slice(0, start) + once(region, '        content: true,',
    '        content: true,\n        image: true,\n        childrenPost: { where: { deletedAt: null }, select: { id: true } },') + t.slice(end);
});
edit('apps/backend/src/public-api/routes/v1/public.integrations.controller.ts', t => once(t,
  '        identifier: integration.providerIdentifier,',
  '        identifier: integration.providerIdentifier,\n        accountId: integration.internalId,\n        refreshNeeded: integration.refreshNeeded,'));
fs.copyFileSync('/adapter/max-browser.provider.ts', root + '/libraries/nestjs-libraries/src/integrations/social/max-browser.provider.ts');
fs.copyFileSync('/adapter/max-browser.guard.ts', root + '/apps/backend/src/max-browser.guard.ts');
const frontend = root + '/apps/frontend/src/components/new-launch/providers/max-browser';
fs.mkdirSync(frontend, { recursive: true });
fs.copyFileSync('/adapter/max-browser.editor.tsx', frontend + '/max-browser.provider.tsx');
fs.copyFileSync('/adapter/max-browser.png', root + '/apps/frontend/public/icons/platforms/max-browser.png');
// Reuse the pinned image's own self-hosted fonts: no Google request at build time.
const staticRoot = root + '/apps/frontend/.next/static';
const faces = new Set();
for (const file of fs.readdirSync(staticRoot + '/chunks').filter(f => f.endsWith('.css'))) {
  const css = fs.readFileSync(staticRoot + '/chunks/' + file, 'utf8');
  for (const match of css.matchAll(/@font-face\{[^}]*Jakarta[^}]*\}/g)) faces.add(match[0]);
}
if (!faces.size) throw new Error('Bundled Jakarta font definitions missing');
const fontRoot = root + '/apps/frontend/public/bridge-fonts';
fs.mkdirSync(fontRoot, { recursive: true });
const styles = [...faces].map(css => css.replace(/\.\.\/media\/([^\)]+)/g, (_, name) => {
  if (!/^[a-zA-Z0-9_.-]+\.woff2$/.test(name)) throw new Error('Unexpected font asset');
  fs.copyFileSync(staticRoot + '/media/' + name, fontRoot + '/' + name);
  return '/bridge-fonts/' + name;
}));
fs.writeFileSync(root + '/apps/frontend/src/bridge-fonts.css', styles.join('\n') +
  '\n.postiz-bridge-font{font-family:"Plus Jakarta Sans","Plus Jakarta Sans Fallback",Arial,sans-serif}\n');
for (const name of ['apps/frontend/src/app/(app)/layout.tsx',
                   'apps/frontend/src/app/(provider)/layout.tsx',
                   'apps/frontend/src/app/(extension)/layout.tsx',
                   'apps/frontend/src/components/new-layout/layout.component.tsx']) {
  edit(name, t => once(t, "import { Plus_Jakarta_Sans } from 'next/font/google';",
    "import '../../bridge-fonts.css';").replace(/const jakartaSans = Plus_Jakarta_Sans\(\{[\s\S]*?\}\);/,
    'const jakartaSans = { className: "postiz-bridge-font", style: { fontFamily: "Plus Jakarta Sans, Arial, sans-serif" } };'));
}
console.log('MAX browser provider source patch applied against pinned checksums');
