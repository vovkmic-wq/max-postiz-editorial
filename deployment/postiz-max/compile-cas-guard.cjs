// Compile only the guarded source module; preserve the repaired backend/frontend.
const fs = require('node:fs'), crypto = require('node:crypto');
const ts = require('/app/node_modules/typescript');
const manifest = JSON.parse(fs.readFileSync('/adapter-cas/cas-manifest.json', 'utf8'));
const sourcePath = '/app/apps/backend/src/max-browser.guard.ts';
const compiledPath = '/app/apps/backend/dist/apps/backend/src/max-browser.guard.js';
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
if (manifest.contract !== 'max-browser-cas-draft-schedule-v1' ||
    !/^sha256:[a-f0-9]{64}$/.test(manifest.base_image_id)) throw new Error('CAS build manifest scope mismatch');
if (hash(fs.readFileSync(sourcePath)) !== manifest.source_sha256 ||
    hash(fs.readFileSync(compiledPath)) !== manifest.compiled_sha256) {
  throw new Error('Pinned CAS guard base mismatch');
}
const source = fs.readFileSync('/adapter-cas/max-browser.guard.ts', 'utf8');
if (hash(source) !== manifest.target_source_sha256) throw new Error('Pinned CAS replacement source mismatch');
const compiled = ts.transpileModule(source, { fileName: 'max-browser.guard.ts', reportDiagnostics: true,
  compilerOptions: { target: ts.ScriptTarget.ES2017, module: ts.ModuleKind.CommonJS,
    experimentalDecorators: true, emitDecoratorMetadata: true, importHelpers: true,
    esModuleInterop: true, sourceMap: false } });
if ((compiled.diagnostics || []).some(d => d.category === ts.DiagnosticCategory.Error)) {
  throw new Error('CAS guard TypeScript compilation failed');
}
let output = compiled.outputText;
const alias = '@gitroom/nestjs-libraries/';
if (output.split(alias).length !== 3) throw new Error('Unexpected CAS guard imports');
output = output.split(alias).join('../../../libraries/nestjs-libraries/src/');
if (output.includes('@gitroom/') || !output.includes('MaxBrowserMutationGuard') ||
    !output.includes('MAX_BROWSER_CAS_SCHEDULE_INVALID') || !output.includes('MAX_BROWSER_CAS_SCHEDULE_CHANGED')) {
  throw new Error('CAS guard unresolved import or missing export');
}
if (hash(output) !== manifest.target_compiled_sha256) throw new Error('Pinned CAS replacement compiled mismatch');
fs.writeFileSync(sourcePath, source);
fs.writeFileSync(compiledPath, output);
fs.writeFileSync('/adapter-cas/receipt.json', JSON.stringify({
  contract: manifest.contract, source_sha256: hash(source), compiled_sha256: hash(output),
  base_source_sha256: manifest.source_sha256, base_compiled_sha256: manifest.compiled_sha256,
  base_image_id: manifest.base_image_id, target_image: manifest.target_image
}, null, 2));
console.log('CAS guard source and compiled module updated; other modules unchanged');
