const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const sourceDir = 'apps/backend/src/public-api/routes/v1';
const compiledDir = 'apps/backend/dist/apps/backend/src/public-api/routes/v1';
const controller = 'public.integrations.controller';
const helper = 'max-schedule-journal';
const routes = `  @Get('/max-schedule-operations/capabilities')
  @Header('Cache-Control', 'no-store')
  async maxScheduleCapabilities(@GetOrgFromRequest() org: Organization, @Headers('x-max-bridge-key') key: string) {
    return scheduleCapabilities(this._schedulePrisma, org.id, key);
  }

  @Get('/max-schedule-operations/:id')
  @Header('Cache-Control', 'no-store')
  async maxScheduleReceipt(@GetOrgFromRequest() org: Organization, @Headers('x-max-bridge-key') key: string,
      @Param('id') id: string) {
    return scheduleReceipt(this._schedulePrisma, org.id, key, id);
  }

  @Post('/max-schedule-operations/:id')
  @CheckPolicies([AuthorizationActions.Create, Sections.POSTS_PER_MONTH])
  async maxScheduleApply(@GetOrgFromRequest() org: Organization, @Headers('x-max-bridge-key') key: string,
      @Param('id') id: string, @Body() binding: unknown) {
    return changeSchedule(this._schedulePrisma, org.id, key, id, binding, 'schedule',
        postId => this._postsService.startWorkflow('max', postId, org.id, 'QUEUE'));
  }

  @Post('/max-schedule-operations/:id/retire')
  async maxScheduleRetire(@GetOrgFromRequest() org: Organization, @Headers('x-max-bridge-key') key: string,
      @Param('id') id: string, @Body() binding: unknown) {
    return changeSchedule(this._schedulePrisma, org.id, key, id, binding, 'retire');
  }

`;

function replaceOnce(input, anchor, replacement) {
  if (input.split(anchor).length !== 2) throw new Error('Unexpected public controller layout');
  return input.replace(anchor, replacement);
}

function compile(root, ts) {
  const sourcePath = path.join(root, sourceDir, controller + '.ts');
  const compiledPath = path.join(root, compiledDir, controller + '.js');
  const manifest = require('./schedule-journal-manifest.json');
  const hash = value => crypto.createHash('sha256').update(value).digest('hex');
  const before = fs.readFileSync(sourcePath, 'utf8');
  if (hash(before) !== manifest.controller_source_sha256 ||
      hash(fs.readFileSync(compiledPath)) !== manifest.controller_compiled_sha256) {
    throw new Error('Pinned public controller base mismatch');
  }
  const imports = `import { PrismaService } from '@gitroom/nestjs-libraries/database/prisma/prisma.service';
import { changeSchedule, scheduleCapabilities, scheduleReceipt } from './${helper}';
`;
  let source = replaceOnce(before, '  Get,\n', '  Get,\n  Header,\n  Headers,\n');
  source = replaceOnce(source, '    private _subscriptionService: SubscriptionService',
    '    private _subscriptionService: SubscriptionService,\n    private _schedulePrisma: PrismaService');
  source = imports + replaceOnce(source, "  @Get('/posts')", routes + "  @Get('/posts')");
  const helperSource = fs.readFileSync(path.join(__dirname, helper + '.ts'), 'utf8');
  function transpile(value, filename) {
    const result = ts.transpileModule(value, { fileName: filename, reportDiagnostics: true,
      compilerOptions: { target: ts.ScriptTarget.ES2017, module: ts.ModuleKind.CommonJS,
        experimentalDecorators: true, emitDecoratorMetadata: true, importHelpers: true, esModuleInterop: true } });
    if (result.diagnostics.some(item => item.category === ts.DiagnosticCategory.Error)) {
      throw new Error('Schedule journal TypeScript compilation failed');
    }
    const output = result.outputText.replaceAll('@gitroom/nestjs-libraries/', '../../../../../../libraries/nestjs-libraries/src/')
      .replaceAll('@gitroom/helpers/', '../../../../../../libraries/helpers/src/')
      .replaceAll('@gitroom/backend/', '../../../');
    if (output.includes('@gitroom/')) throw new Error('Schedule journal unresolved module');
    return output;
  }
  const compiledController = transpile(source, controller + '.ts');
  const compiledHelper = transpile(helperSource, helper + '.ts');
  fs.writeFileSync(sourcePath, source);
  fs.writeFileSync(compiledPath, compiledController);
  fs.writeFileSync(path.join(root, sourceDir, helper + '.ts'), helperSource);
  fs.writeFileSync(path.join(root, compiledDir, helper + '.js'), compiledHelper);
}

module.exports = { compile };
if (require.main === module) compile('/app', require('/app/node_modules/typescript'));
