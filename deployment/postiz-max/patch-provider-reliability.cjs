// Меняет только исходник и две копии провайдера в проверенном готовом образе.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const hash = value => crypto.createHash('sha256').update(value).digest('hex');

function planPatch(root, adapter, ts) {
  const manifest = JSON.parse(fs.readFileSync(path.join(adapter, 'provider-reliability-manifest.json')));
  if (ts.version !== manifest.typescript_version) throw new Error('PROVIDER_COMPILER_VERSION_MISMATCH');
  for (const [relative, expected] of Object.entries({ [manifest.source]: manifest.source_sha256, ...manifest.compiled })) {
    if (hash(fs.readFileSync(path.join(root, relative))) !== expected) throw new Error('PROVIDER_BASE_MISMATCH');
  }
  const source = fs.readFileSync(path.join(adapter, 'max-browser.provider.ts'), 'utf8');
  const compiled = ts.transpileModule(source, { fileName: manifest.source, reportDiagnostics: true,
    compilerOptions: { target: ts.ScriptTarget.ES2017, module: ts.ModuleKind.CommonJS,
      experimentalDecorators: true, emitDecoratorMetadata: true, importHelpers: true, esModuleInterop: true } });
  if ((compiled.diagnostics || []).some(item => item.category === ts.DiagnosticCategory.Error)) {
    throw new Error('PROVIDER_TYPESCRIPT_INVALID');
  }
  const output = compiled.outputText
    .replace(/require\("@gitroom\/nestjs-libraries\/integrations\/social\.abstract"\)/g, 'require("../social.abstract")')
    .replace(/require\("@gitroom\/nestjs-libraries\/services\/make\.secure\.id"\)/g, 'require("../../services/make.secure.id")');
  if (output.includes('@gitroom/')) throw new Error('PROVIDER_UNRESOLVED_ALIAS');
  return [[manifest.source, source], ...Object.keys(manifest.compiled).map(relative => [relative, output])];
}
function applyPatch(root, adapter, ts) {
  const planned = planPatch(root, adapter, ts);
  for (const [relative, content] of planned) fs.writeFileSync(path.join(root, relative), content);
  return Object.fromEntries(planned.map(([relative, content]) => [relative, hash(content)]));
}
module.exports = { planPatch, applyPatch };
if (require.main === module) {
  const root = process.argv[2] || '/app';
  console.log(JSON.stringify({ provider_files: applyPatch(root, __dirname, require(path.join(root, 'node_modules/typescript'))) }));
}
