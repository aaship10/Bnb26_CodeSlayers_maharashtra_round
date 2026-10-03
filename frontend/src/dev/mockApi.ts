import { z } from 'zod';
import { serverClock } from '@/lib/serverClock';

/** Client for the mock server's /__mock control endpoints. Dev tooling only. */
const stateSchema = z.object({
  server_now: z.string(),
  speed: z.number(),
  scenario: z.string(),
  scenarios: z.array(
    z.object({ id: z.string(), name: z.string(), description: z.string(), group: z.enum(['Timeline', 'Outcomes', 'Failures']) }),
  ),
  time_presets: z.array(z.object({ id: z.string(), label: z.string(), iso: z.string() })),
  faults: z.array(z.string()),
  fault_targets: z.array(z.string()),
  fault_kinds: z.array(z.string()),
  sse_clients: z.number(),
  sse_enabled: z.boolean(),
  invariants_broken: z.boolean(),
  tamper: z.string(),
  tamper_modes: z.array(z.string()),
  admin_token: z.string(),
  otp: z.string(),
  captcha_token: z.string(),
  timeline: z.object({ opens: z.string(), closes: z.string(), hold_ends: z.string() }),
});

export type MockState = z.infer<typeof stateSchema>;

async function call(path: string, body?: unknown): Promise<unknown> {
  const res = await fetch(`/__mock${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`mock ${path} -> HTTP ${res.status}`);
  return res.json();
}

const parseState = (raw: unknown) => stateSchema.parse(raw);

/** After a time jump the old server-clock samples are wrong; forget them so the next response re-syncs. */
const resync = (s: MockState) => {
  serverClock.reset();
  return s;
};

export const mockApi = {
  state: () => call('/state').then(parseState),
  scenario: (id: string) => call('/scenario', { id }).then(parseState).then(resync),
  reset: () => call('/reset', {}).then(parseState).then(resync),
  clockSet: (iso: string) => call('/clock', { set: iso }).then(parseState).then(resync),
  clockAdvance: (ms: number) => call('/clock', { advance_ms: ms }).then(parseState).then(resync),
  clockSpeed: (speed: number) => call('/clock', { speed }).then(parseState).then(resync),
  faults: (faults: string[]) => call('/faults', { faults }).then(parseState),
  dropSse: () => call('/sse/drop', {}),
  setSse: (enabled: boolean) => call('/sse', { enabled }).then(parseState),
  breakInvariants: (broken: boolean) => call('/invariants', { broken }),
  tamper: (mode: string) => call('/tamper', { mode }),
};
