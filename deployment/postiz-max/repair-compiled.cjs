const fs = require('node:fs'), crypto = require('node:crypto');
const manifest = JSON.parse(fs.readFileSync('/adapter/compiled-manifest.json', 'utf8'));
for (const [name, hash] of Object.entries(manifest.files)) {
  const file = '/app/' + name, source = fs.readFileSync(file, 'utf8');
  if (crypto.createHash('sha256').update(source).digest('hex') !== hash) throw new Error('Compiled source mismatch: ' + name);
  let result;
  if (name.endsWith('temporal.module.js')) {
    const anchor = ".filter((f) => f.identifier.indexOf('-') === -1)";
    if (source.split(anchor).length !== 2) throw new Error('Queue registry anchor missing');
    result = source.replace(anchor, ".filter((f) => f.identifier.indexOf('-') === -1 || f.identifier === 'max-browser')");
  } else if (name.endsWith('integration.manager.js') || name.endsWith('app.module.js')) {
    const line = source.split('\n').find(s => /const max_browser_(provider|guard)_1 = require\(/.test(s));
    if (!line) throw new Error('Missing compiled provider import');
    result = source.replace(line + '\n', '').replace('"use strict";\n', '"use strict";\n' + line + '\n');
  } else {
    const anchor = 'identifier: integration.providerIdentifier,';
    if (source.split(anchor).length !== 2) throw new Error('Ambiguous integration projection');
    result = source.replace(anchor, anchor + '\n              accountId: integration.internalId,\n              refreshNeeded: integration.refreshNeeded,');
  }
  fs.writeFileSync(file, result);
}
console.log('Pinned compiled modules corrected; frontend remains the fully built source artifact');
