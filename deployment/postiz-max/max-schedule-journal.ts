import { ConflictException, ForbiddenException, ServiceUnavailableException } from '@nestjs/common';
import { timingSafeEqual } from 'node:crypto';
import type { PrismaService } from '@gitroom/nestjs-libraries/database/prisma/prisma.service';
import { maxBridgeRequest } from '@gitroom/nestjs-libraries/integrations/social/max-browser.provider';

const contract = 'max-schedule-journal-v1';
const operationId = /^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const ids = ['post_id', 'group_id', 'integration_id', 'record_id', 'variant_id', 'slot_id'];
const hashes = ['expected_hash', 'content_sha256', 'image_json_sha256', 'settings_sha256'];
const dates = ['draft_due_ms', 'due_ms', 'deadline_ms'];

function authorize(org: string, key: unknown) {
  const configured = process.env.MAX_POSTIZ_BRIDGE_KEY;
  if (!org || org !== process.env.EDITORIAL_OWNER_ORGANIZATION_ID || !configured || typeof key !== 'string') {
    throw new ForbiddenException('MAX_SCHEDULE_ACCESS_DENIED');
  }
  const expected = Buffer.from(configured);
  const actual = Buffer.from(key);
  if (expected.length !== actual.length || !timingSafeEqual(expected, actual)) {
    throw new ForbiddenException('MAX_SCHEDULE_ACCESS_DENIED');
  }
}

function operation(id: string, input: any) {
  if (!operationId.test(id) || !input || typeof input !== 'object' || Array.isArray(input)
      || Object.keys(input).length !== 1 + ids.length + hashes.length + dates.length
      || input.operation_id !== id
      || !ids.every(key => typeof input[key] === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(input[key]))
      || !hashes.every(key => typeof input[key] === 'string' && /^[a-f0-9]{64}$/.test(input[key]))
      || !dates.every(key => Number.isSafeInteger(input[key]) && input[key] >= 0 && input[key] < 253402300800000)
      || input.deadline_ms >= input.due_ms) {
    throw new ConflictException('MAX_SCHEDULE_INPUT_INVALID');
  }
  return input;
}

async function query<T>(prisma: PrismaService, sql: string, ...values: unknown[]): Promise<T> {
  try {
    return await prisma.$queryRawUnsafe<T>(sql, ...values);
  } catch {
    // Do not send PostgreSQL statements, bindings or connection information.
    throw new ServiceUnavailableException('MAX_SCHEDULE_JOURNAL_UNAVAILABLE');
  }
}

export async function scheduleCapabilities(prisma: PrismaService, org: string, key: unknown) {
  authorize(org, key);
  const [result] = await query<{ ready: boolean }[]>(prisma,
    `SELECT to_regclass('max_schedule_journal') IS NOT NULL
      AND to_regprocedure('max_schedule_change(text,jsonb,text)') IS NOT NULL
      AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass('"Post"')
        AND tgname = 'max_schedule_retired_draft' AND tgenabled = 'O') AS ready`);
  if (!result?.ready) throw new ServiceUnavailableException('MAX_SCHEDULE_JOURNAL_UNAVAILABLE');
  return { contract };
}

export async function scheduleReceipt(prisma: PrismaService, org: string, key: unknown, id: string) {
  authorize(org, key);
  if (!operationId.test(id)) throw new ConflictException('MAX_SCHEDULE_INPUT_INVALID');
  const [row] = await query<any[]>(prisma,
    `SELECT binding AS operation, outcome, scheduled_at, retired_at FROM max_schedule_journal
      WHERE organization_id = $1 AND operation_id = $2`, org, id);
  return row || { outcome: 'unrecorded' };
}

export async function changeSchedule(prisma: PrismaService, org: string, key: unknown, id: string,
    input: unknown, action: 'schedule' | 'retire', startWorkflow?: (postId: string) => Promise<unknown>) {
  authorize(org, key);
  const binding = operation(id, input);
  if (action === 'schedule' && (Date.now() >= binding.deadline_ms || binding.deadline_ms > Date.now() + 21000)) {
    throw new ConflictException('MAX_SCHEDULE_DEADLINE');
  }
  let proof;
  try {
    proof = await maxBridgeRequest(action === 'schedule' ? '/schedule/start' : '/schedule/retire', binding);
  } catch {
    throw new ServiceUnavailableException('MAX_SCHEDULE_BRIDGE_UNAVAILABLE');
  }
  if (action === 'schedule' ? proof?.owner !== id : proof?.retired !== true) {
    throw new ConflictException('MAX_SCHEDULE_BRIDGE_UNCONFIRMED');
  }
  const [row] = await query<{ result: { outcome: string; new_schedule?: boolean } }[]>(prisma,
    'SELECT max_schedule_change($1::text, $2::jsonb, $3::text) AS result',
    org, JSON.stringify(binding), action);
  const result = row?.result;
  if (action === 'schedule' && result?.outcome !== 'scheduled') {
    throw new ConflictException('MAX_SCHEDULE_NOT_APPLIED');
  }
  if (action === 'retire' && !['draft_preserved', 'draft_restored', 'draft_conflict'].includes(result?.outcome)) {
    throw new ConflictException('MAX_SCHEDULE_RETIRE_NOT_READY');
  }
  if (result.new_schedule && startWorkflow) {
    // Same workflow as the native scheduling route, with one start per committed write.
    void startWorkflow(binding.post_id).catch(() => {});
  }
  return { operation: binding, outcome: result.outcome };
}
