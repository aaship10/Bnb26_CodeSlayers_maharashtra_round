/**
 * Validates REAL backend responses with the frontend's OWN zod schemas (Member D's code, read-only).
 * If a response drifts from what the UI parses, this fails before a human sees a blank page.
 *
 *   python infra/local/run.py up
 *   python infra/local/run.py exec -- ../frontend/node_modules/.bin/tsx.cmd --tsconfig ../frontend/tsconfig.json scripts/frontend_contract_check.ts
 *
 * (cwd = Member_B_Pipeline; env comes from infra/.env through `run.py exec`.)
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { z } from 'zod';
import {
  enterResponseSchema,
  errorBodySchema,
  eventListSchema,
  eventSchema,
  meSchema,
  sessionResponseSchema,
  statusSchema,
} from '../../frontend/src/api/schemas';
import { presetListSchema } from '../../frontend/src/features/admin/schemas';

const BASE = (process.env.E2E_BASE ?? 'http://127.0.0.1:8080') + '/api';
const OUTBOX = process.env.OUTBOX_DIR ?? join(process.cwd(), '.local', 'outbox');
const ADMIN = process.env.ADMIN_TOKEN ?? '';
const EVENT = '11111111-1111-1111-1111-111111111111';
const results: { name: string; ok: boolean; detail?: string }[] = [];

function record(name: string, ok: boolean, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${!ok && detail ? `  [${detail}]` : ''}`);
}

async function call(path: string, init: RequestInit & { json?: unknown; token?: string; admin?: string } = {}) {
  const headers = new Headers(init.headers);
  headers.set('X-Device-Id', DEVICE);
  if (init.json !== undefined) headers.set('Content-Type', 'application/json');
  if (init.token) headers.set('Authorization', `Bearer ${init.token}`);
  if (init.admin) headers.set('X-Admin-Token', init.admin);
  const res = await fetch(BASE + path, { ...init, headers, body: init.json !== undefined ? JSON.stringify(init.json) : init.body });
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  return { status: res.status, headers: res.headers, body };
}

function parses(name: string, schema: z.ZodTypeAny, value: unknown) {
  const r = schema.safeParse(value);
  record(name, r.success, r.success ? '' : JSON.stringify(r.error.issues.slice(0, 3)));
  return r.success ? r.data : undefined;
}

function otpFor(email: string, since: number): string {
  const deadline = Date.now() + 6000;
  while (Date.now() < deadline) {
    const files = readdirSync(OUTBOX)
      .map((f) => join(OUTBOX, f))
      .filter((f) => statSync(f).mtimeMs >= since - 1000)
      .sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs);
    for (const f of files) {
      const m = JSON.parse(readFileSync(f, 'utf8')) as { to: string; body: string };
      if (m.to === email) return /(\d{6})/.exec(m.body)![1]!;
    }
  }
  throw new Error(`no OTP mail for ${email}`);
}

const DEVICE = crypto.randomUUID();

async function main() {
  const tag = Math.random().toString(16).slice(2, 8);
  const email = `contract.${tag}@example-college.edu`;

  // sign-in, exactly the calls RegisterPage makes
  const since = Date.now();
  const reg = await call('/auth/register', { method: 'POST', json: { email, display_name: 'Contract Check', hp: '' } });
  record('POST /auth/register -> 202', reg.status === 202, String(reg.status));
  const ver = await call('/auth/verify', { method: 'POST', json: { email, otp: otpFor(email, since) } });
  const session = parses('POST /auth/verify matches sessionResponseSchema', sessionResponseSchema, ver.body);
  if (!session) return finish();
  const token = session.token;

  parses('GET /auth/me matches meSchema', meSchema, (await call('/auth/me', { token })).body);
  parses('POST /auth/refresh matches sessionResponseSchema', sessionResponseSchema, (await call('/auth/refresh', { method: 'POST', token })).body);

  // event pages
  parses('GET /events matches eventListSchema', eventListSchema, (await call('/events')).body);
  parses('GET /events/{id} matches eventSchema', eventSchema, (await call(`/events/${EVENT}`)).body);

  const first = await call(`/events/${EVENT}/enter`, { method: 'POST', token });
  const e1 = parses('POST /enter matches enterResponseSchema', enterResponseSchema, first.body);
  const second = await call(`/events/${EVENT}/enter`, { method: 'POST', token });
  const e2 = enterResponseSchema.safeParse(second.body);
  record('repeat /enter is idempotent (already_entered=true)', e2.success && e2.data.already_entered === true && e1?.already_entered === false);
  record('enter body has no allocation weight', !JSON.stringify(first.body).includes('weight'));

  parses('GET /status matches statusSchema', statusSchema, (await call(`/events/${EVENT}/status`, { token })).body);

  // error bodies the UI maps to copy
  const bad = await call('/auth/register', { method: 'POST', json: { email: 'x@evil.test', display_name: 'x', hp: '' } });
  parses('422 body matches errorBodySchema', errorBodySchema, bad.body);
  record('422 is VALIDATION_ERROR', (bad.body as { code?: string }).code === 'VALIDATION_ERROR');
  const noauth = await call(`/events/${EVENT}/status`);
  parses('401 body matches errorBodySchema', errorBodySchema, noauth.body);

  // 429: switch the layer on, hammer, read back what the UI would read
  if (ADMIN) {
    const presets = await call('/admin/defence/presets', { admin: ADMIN });
    const list = parses('GET /admin/defence/presets matches presetListSchema', presetListSchema, presets.body);
    record('presets include all six ids', list?.length === 6, String(list?.length));
    const patch = await call(`/admin/events/${EVENT}/config`, { method: 'PATCH', admin: ADMIN, json: { defences: { preset: 'rate_limit' } } });
    record('PATCH config preset=rate_limit', patch.status === 200, JSON.stringify(patch.body));
    await new Promise((r) => setTimeout(r, 2200)); // config cache TTL is ~1.5 s per replica
    let limited: { status: number; headers: Headers; body: unknown } | undefined;
    for (let i = 0; i < 12 && !limited; i++) {
      const r = await call(`/events/${EVENT}/status`, { token });
      if (r.status === 429) limited = r;
    }
    record('flood eventually gets 429', !!limited);
    if (limited) {
      parses('429 body matches errorBodySchema', errorBodySchema, limited.body);
      const d = (limited.body as { details?: { retry_after_ms?: number; scope?: string } }).details;
      record('429 carries details.retry_after_ms + scope', typeof d?.retry_after_ms === 'number' && typeof d?.scope === 'string');
      record('429 carries Retry-After header', Number(limited.headers.get('retry-after')) >= 1);
    }
    await call(`/admin/events/${EVENT}/config`, { method: 'PATCH', admin: ADMIN, json: { defences: { preset: 'none' } } });
  } else {
    console.log('(ADMIN_TOKEN not set: skipping admin/429 checks; run via `run.py exec`)');
  }
  finish();
}

function finish() {
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} contract checks passed`);
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => {
  console.error('FAIL  unexpected error:', e);
  process.exit(1);
});
