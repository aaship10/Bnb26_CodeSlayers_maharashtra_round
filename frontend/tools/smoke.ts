/**
 * Contract smoke test against a RUNNING stack (real A/B/C behind nginx, or the mock).
 * Hits every endpoint the UI depends on and validates each response with the
 * same zod schemas the app uses, so integration problems show up as a list of
 * field-level mismatches instead of broken screens.
 *
 *   npm run smoke -- --base http://localhost:8080 --event evt_demo_01 --admin-token $ADMIN_TOKEN
 *   npm run smoke -- --base http://localhost:5173 --dev-user 00000000-0000-4000-8000-000000000001
 *
 * Read-only by default. --writes also enters the event and starts a mock sim run.
 */
import { z, type ZodTypeAny } from 'zod';
import { challengeSchema, enterResponseSchema, errorBodySchema, eventListSchema, eventSchema, meSchema, statusSchema } from '../src/api/schemas';
import { adminEventListSchema, invariantsSchema, presetListSchema, statsSchema } from '../src/features/admin/schemas';
import { auditPageSchema, auditVerifySchema, entrantsSchema, fairnessSchema } from '../src/features/fairness/schemas';
import { chartSchema, experimentSchema, scenarioSchema } from '../src/features/sim/schemas';
import { createSseParser } from '../src/features/live/sseParser';

const args = new Map<string, string>();
for (let i = 2; i < process.argv.length; i++) {
  const a = process.argv[i]!;
  if (a.startsWith('--')) args.set(a.slice(2), process.argv[i + 1]?.startsWith('--') || process.argv[i + 1] === undefined ? 'true' : process.argv[++i]!);
}
const BASE = (args.get('base') ?? 'http://localhost:8080').replace(/\/$/, '');
const EVENT = args.get('event');
const ADMIN = args.get('admin-token') ?? process.env.ADMIN_TOKEN;
const TOKEN = args.get('token');
const DEV_USER = args.get('dev-user');
const WRITES = args.get('writes') === 'true';

type Result = { name: string; ok: boolean; detail: string };
const results: Result[] = [];
const auth: Record<string, string> = { 'X-Device-Id': 'smoke-test-device' };
if (TOKEN) auth.Authorization = `Bearer ${TOKEN}`;
else if (DEV_USER) auth['X-User-Id'] = DEV_USER;

async function check(name: string, path: string, schema: ZodTypeAny | null, init: RequestInit & { expect?: number[]; anonymous?: boolean } = {}) {
  const t0 = Date.now();
  try {
    const identity = init.anonymous ? { 'X-Device-Id': 'smoke-test-device' } : auth;
    const res = await fetch(BASE + path, { ...init, headers: { Accept: 'application/json', ...identity, ...(init.headers ?? {}) } });
    const text = await res.text();
    let json: unknown;
    try {
      json = text ? JSON.parse(text) : undefined;
    } catch {
      json = undefined;
    }
    const expected = init.expect ?? [200];
    if (!expected.includes(res.status)) {
      const err = errorBodySchema.safeParse(json);
      results.push({ name, ok: false, detail: `HTTP ${res.status}${err.success ? ` ${err.data.code}: ${err.data.message}` : ` (body is not {code,message}: ${text.slice(0, 80)})`}` });
      return undefined;
    }
    if (res.status >= 400) {
      const err = errorBodySchema.safeParse(json);
      results.push({ name, ok: err.success, detail: err.success ? `HTTP ${res.status} ${err.data.code} as expected` : 'error body is not {code,message,details?}' });
      return undefined;
    }
    if (!schema) {
      results.push({ name, ok: true, detail: `HTTP ${res.status} (${Date.now() - t0} ms)` });
      return json;
    }
    const parsed = schema.safeParse(json);
    if (!parsed.success) {
      const issues = parsed.error.issues.slice(0, 4).map((i) => `${i.path.join('.') || '(root)'}: ${i.message}`);
      results.push({ name, ok: false, detail: `shape mismatch: ${issues.join('; ')}` });
      return undefined;
    }
    results.push({ name, ok: true, detail: `HTTP ${res.status}, schema ok (${Date.now() - t0} ms)` });
    return parsed.data;
  } catch (e) {
    results.push({ name, ok: false, detail: `unreachable: ${e instanceof Error ? e.message : String(e)}` });
    return undefined;
  }
}

async function checkStream(name: string, path: string, eventName: string) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 6000);
  try {
    const res = await fetch(BASE + path, { headers: { Accept: 'text/event-stream', ...auth }, signal: ctl.signal });
    const type = res.headers.get('content-type') ?? '';
    if (!res.ok || !type.includes('text/event-stream')) {
      results.push({ name, ok: false, detail: `HTTP ${res.status}, content-type "${type}"` });
      return;
    }
    let got: { id?: string; event: string } | null = null;
    const parser = createSseParser((m) => {
      if (!got && m.event === eventName) got = m;
    });
    const reader = res.body!.getReader();
    const dec = new TextDecoder();
    while (!got) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.push(dec.decode(value, { stream: true }));
    }
    ctl.abort();
    const g = got as { id?: string; event: string } | null;
    results.push({
      name,
      ok: !!g,
      detail: g ? `first "${eventName}" event arrived${g.id ? ` with id ${g.id} (resumable)` : ' WITHOUT an id (Last-Event-ID resume impossible)'}` : `no "${eventName}" event within 6 s (proxy buffering?)`,
    });
  } catch (e) {
    results.push({ name, ok: false, detail: ctl.signal.aborted ? `no "${eventName}" event within 6 s (proxy buffering?)` : String(e) });
  } finally {
    clearTimeout(timer);
  }
}

async function main() {
  console.log(`Fair Drop contract smoke test against ${BASE}\n`);

  // routing (nginx/Vite): SPA fallback, /__mock blocked in real deployments
  const deep = await fetch(`${BASE}/events/x/status`).catch(() => null);
  results.push({ name: 'SPA deep link falls back to index.html', ok: !!deep && deep.ok && (deep.headers.get('content-type') ?? '').includes('text/html'), detail: deep ? `HTTP ${deep.status}` : 'unreachable' });

  const events = (await check('GET /api/events', '/api/events', eventListSchema)) as z.infer<typeof eventListSchema> | undefined;
  const eventId = EVENT ?? events?.[0]?.id;
  if (!eventId) {
    results.push({ name: 'pick an event', ok: false, detail: 'no events returned; pass --event' });
  } else {
    await check(`GET /api/events/${eventId}`, `/api/events/${eventId}`, eventSchema);
    if (TOKEN || DEV_USER) {
      await check('GET /api/auth/me', '/api/auth/me', meSchema, { expect: [200, 404] });
      await check('GET status', `/api/events/${eventId}/status`, statusSchema);
      await checkStream('status SSE stream (headers auth)', `/api/events/${eventId}/stream`, 'status');
      await check('POST /api/defence/challenge', '/api/defence/challenge', challengeSchema, {
        method: 'POST',
        body: JSON.stringify({ event_id: eventId }),
        headers: { 'Content-Type': 'application/json' },
        expect: [200, 404],
      });
      if (WRITES) {
        await check('POST enter (idempotent)', `/api/events/${eventId}/enter`, enterResponseSchema, { method: 'POST', expect: [200, 409, 403, 429] });
      }
    } else {
      results.push({ name: 'authenticated attendee checks', ok: true, detail: 'skipped (pass --token or --dev-user)' });
    }
    await check('status without auth is UNAUTHENTICATED', `/api/events/${eventId}/status`, null, { anonymous: true, expect: [401] });

    const f = (await check('GET fairness', `/api/events/${eventId}/fairness`, fairnessSchema)) as z.infer<typeof fairnessSchema> | undefined;
    if (f?.entrants_hash) await check('GET fairness/entrants', `/api/events/${eventId}/fairness/entrants`, entrantsSchema);
    await check('GET audit (page 1)', `/api/events/${eventId}/audit?from_seq=1`, auditPageSchema);
    await check('GET audit/verify', `/api/events/${eventId}/audit/verify`, auditVerifySchema);

    if (ADMIN) {
      const h = { 'X-Admin-Token': ADMIN };
      await check('GET admin/defence/presets', '/api/admin/defence/presets', presetListSchema, { headers: h });
      await check('GET admin/events (A10)', '/api/admin/events', adminEventListSchema, { headers: h, expect: [200, 404] });
      await check('GET admin stats', `/api/admin/events/${eventId}/stats`, statsSchema, { headers: h });
      await check('GET admin invariants', `/api/admin/events/${eventId}/invariants`, invariantsSchema, { headers: h });
      await check('admin with a wrong token is refused', '/api/admin/defence/presets', null, { headers: { 'X-Admin-Token': 'definitely-wrong' }, expect: [401, 403] });
    } else {
      results.push({ name: 'admin checks', ok: true, detail: 'skipped (pass --admin-token)' });
    }
  }

  const scenarios = (await check('GET /sim/scenarios', '/sim/scenarios', z.array(scenarioSchema))) as z.infer<typeof scenarioSchema>[] | undefined;
  const exps = (await check('GET /sim/experiments', '/sim/experiments', z.array(experimentSchema))) as z.infer<typeof experimentSchema>[] | undefined;
  const e0 = exps?.[0];
  if (e0?.charts[0]) await check(`GET /sim/charts/${e0.charts[0]}`, `/sim/charts/${e0.charts[0]}?experiment=${e0.id}`, chartSchema);
  if (WRITES && scenarios?.[0]) {
    const run = (await check('POST /sim/runs (target mock)', '/sim/runs', z.object({ run_id: z.string() }), {
      method: 'POST',
      body: JSON.stringify({ scenario_id: scenarios[0].id, overrides: {}, repeats: 1, seed: 1, target: 'mock' }),
      headers: { 'Content-Type': 'application/json' },
      expect: [200, 201],
    })) as { run_id: string } | undefined;
    if (run) await checkStream('sim run SSE stream', `/sim/runs/${run.run_id}/stream`, 'status');
  }

  const width = Math.max(...results.map((r) => r.name.length));
  for (const r of results) console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name.padEnd(width)}  ${r.detail}`);
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} checks passed`);
  process.exit(failed ? 1 : 0);
}

void main();
