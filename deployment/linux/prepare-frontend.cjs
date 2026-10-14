// Rebuild the tested MAX image's frontend for its real LAN/VPN origin.
const fs = require('node:fs');
const root = '/app/apps/frontend';
const staticRoot = root + '/.next/static';
const faces = new Set();
for (const file of fs.readdirSync(staticRoot + '/chunks').filter(f => f.endsWith('.css'))) {
  const css = fs.readFileSync(staticRoot + '/chunks/' + file, 'utf8');
  for (const match of css.matchAll(/@font-face\{[^}]*Jakarta[^}]*\}/g)) faces.add(match[0]);
}
if (!faces.size) throw new Error('Pinned image bundled fonts missing');
fs.mkdirSync(root + '/public/bridge-fonts', {recursive: true});
const styles = [...faces].map(css => css.replace(/\.\.\/media\/([^\)]+)/g, (_, name) => {
  if (!/^[a-zA-Z0-9_.-]+\.woff2$/.test(name)) throw new Error('Unexpected font file');
  fs.copyFileSync(staticRoot + '/media/' + name, root + '/public/bridge-fonts/' + name);
  return '/bridge-fonts/' + name;
}));
fs.writeFileSync(root + '/src/bridge-fonts.css', styles.join('\n') + '\n.postiz-bridge-font{font-family:"Plus Jakarta Sans",Arial,sans-serif}');
for (const name of ['src/app/(app)/layout.tsx', 'src/app/(provider)/layout.tsx',
                    'src/app/(extension)/layout.tsx', 'src/components/new-layout/layout.component.tsx']) {
  const p = root + '/' + name;
  const before = fs.readFileSync(p, 'utf8');
  const after = before.replace("import { Plus_Jakarta_Sans } from 'next/font/google';", "import '../../bridge-fonts.css';")
    .replace(/const jakartaSans = Plus_Jakarta_Sans\(\{[\s\S]*?\}\);/,
      'const jakartaSans = { className: "postiz-bridge-font", style: { fontFamily: "Plus Jakarta Sans, Arial, sans-serif" } };');
  if (before === after) throw new Error('Frontend patch anchor missing: ' + name);
  fs.writeFileSync(p, after);
}
const config = root + '/next.config.js';
const before = fs.readFileSync(config, 'utf8');
if (!before.includes('  experimental: {')) throw new Error('Next configuration anchor missing');
fs.writeFileSync(config, before.replace('const nextConfig = {',
  "const nextConfig = {\n  serverExternalPackages: ['isomorphic-dompurify', 'jsdom'],")
  .replace('  experimental: {', '  experimental: {\n    cpus: 2,'));
console.log('Offline fonts and bounded Next build workers configured');
