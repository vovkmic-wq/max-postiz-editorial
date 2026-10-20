// Compile only the guarded source module; preserve the repaired backend/frontend.
const fs = require('node:fs'), crypto = require('node:crypto');
const ts = require('/app/node_modules/typescript');
const manifest = JSON.parse(fs.readFileSync('/adapter-cas/cas-manifest.json', 'utf8'));
const sourcePath = '/app/apps/backend/src/max-browser.guard.ts';
const compiledPath = '/app/apps/backend/dist/apps/backend/src/max-browser.guard.js';
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
if (hash(fs.readFileSync(sourcePath)) !== manifest.source_sha256 ||
    hash(fs.readFileSync(compiledPath)) !== manifest.compiled_sha256) {
  throw new Error('Pinned CAS guard base mismatch');
}
const source = fs.readFileSync('/adapter-cas/max-browser.guard.ts', 'utf8');
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
if (output.includes('@gitroom/') || !output.includes('MaxBrowserMutationGuard')) {
  throw new Error('CAS guard unresolved import or missing export');
}
fs.writeFileSync(sourcePath, source);
fs.writeFileSync(compiledPath, output);
fs.writeFileSync('/adapter-cas/receipt.json', JSON.stringify({
  source_sha256: hash(source), compiled_sha256: hash(output), base_image_id: manifest.base_image_id
}, null, 2));
console.log('CAS guard source and compiled module updated; other modules unchanged');
