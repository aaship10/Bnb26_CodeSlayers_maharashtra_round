/**
 * Contract test: the mock server is only useful if it is faithful to the shared
 * conventions. Every response is validated with the *client's* zod schemas.
 */
import { createHash } from 'node:crypto';
import { buildApp } from './src/app';
import { solvePow, leadingZeroBits } from './src/pow';
import { SCENARIOS } from './src/scenarios';
import {
  challengeSchema,
  claimResponseSchema,
  enterResponseSchema,
  errorBodySchema,
  eventListSchema,
  eventSchema,
  meSchema,
  sessionResponseSchema,
  statusSchema,
} from '../src/api/schemas';

const EVT = 'evt_demo_01';

async function signIn(app: ReturnType<typeof buildApp>['app'], email = 'asha@example.com') {
  const reg = await app.inject({ method: 'POST', url: '/auth/register', payload: { email, display_name: 'Asha', hp: '' } });
  expect(reg.statusCode).toBe(202);
  const ver = await app.inject({ method: 'POST', url: '/auth/verify', payload: { email, otp: '123456' } });
  expect(ver.statusCode).toBe(200);
  const session = sessionResponseSchema.parse(ver.json());
  return { Authorization: `Bearer ${session.token}`, 'X-Device-Id': 'dev-test' };
}

function make(scenario: string) {
  const m = buildApp();
  m.world.reset(scenario);
  return m;
}

describe('every scenario is well-formed', () => {
  it.each(SCENARIOS.map((s) => s.id))('%s serves schema-valid events and status', async (id) => {
    const { app, world } = make(id);
    world.setFaults([]); // this sweep checks shapes; fault behaviour has its own tests below
    const headers = await signIn(app);
    const events = await app.inject({ method: 'GET', url: '/events' });
    expect(eventListSchema.safeParse(events.json()).success).toBe(true);
    const status = await app.inject({ method: 'GET', url: `/events/${EVT}/status`, headers });
    expect(status.statusCode).toBe(200);
    expect(statusSchema.safeParse(status.json()).success).toBe(true);
    await app.close();
  });
});

describe('auth', () => {
  it('register -> verify -> me', async () => {
    const { app } = make('fresh');
    const headers = await signIn(app);
    const me = await app.inject({ method: 'GET', url: '/auth/me', headers });
    expect(meSchema.parse(me.json()).email).toBe('asha@example.com');
    await app.close();
  });

  it('rejects a wrong OTP with VALIDATION_ERROR', async () => {
    const { app } = make('fresh');
    await app.inject({ method: 'POST', url: '/auth/register', payload: { email: 'a@b.co', display_name: 'A', hp: '' } });
    const res = await app.inject({ method: 'POST', url: '/auth/verify', payload: { email: 'a@b.co', otp: '000000' } });
    expect(res.statusCode).toBe(400);
    expect(errorBodySchema.parse(res.json()).code).toBe('VALIDATION_ERROR');
    await app.close();
  });

  it('answers 202 to a filled honeypot but creates nothing', async () => {
    const { app } = make('fresh');
    const reg = await app.inject({ method: 'POST', url: '/auth/register', payload: { email: 'bot@b.co', display_name: 'Bot', hp: 'gotcha' } });
    expect(reg.statusCode).toBe(202);
    const ver = await app.inject({ method: 'POST', url: '/auth/verify', payload: { email: 'bot@b.co', otp: '123456' } });
    expect(ver.statusCode).toBe(400);
    await app.close();
  });

  it('401 UNAUTHENTICATED without credentials', async () => {
    const { app } = make('fresh');
    const res = await app.inject({ method: 'GET', url: `/events/${EVT}/status` });
    expect(res.statusCode).toBe(401);
    expect(errorBodySchema.parse(res.json()).code).toBe('UNAUTHENTICATED');
    await app.close();
  });

  it('accepts X-User-Id in dev auth mode', async () => {
    const { app } = make('window-open');
    const res = await app.inject({ method: 'GET', url: `/events/${EVT}/status`, headers: { 'X-User-Id': 'u-123' } });
    expect(res.statusCode).toBe(200);
    await app.close();
  });
});

describe('enter', () => {
  it('WINDOW_NOT_OPEN before the window', async () => {
    const { app } = make('fresh');
    const res = await app.inject({ method: 'POST', url: `/events/${EVT}/enter`, headers: await signIn(app) });
    expect(res.statusCode).toBe(409);
    expect(errorBodySchema.parse(res.json()).code).toBe('WINDOW_NOT_OPEN');
    await app.close();
  });

  it('enters once and is idempotent', async () => {
    const { app } = make('window-open');
    const headers = await signIn(app);
    const a = enterResponseSchema.parse((await app.inject({ method: 'POST', url: `/events/${EVT}/enter`, headers })).json());
    const b = enterResponseSchema.parse((await app.inject({ method: 'POST', url: `/events/${EVT}/enter`, headers })).json());
    expect(a.already_entered).toBe(false);
    expect(b.already_entered).toBe(true);
    expect(b.entered_at).toBe(a.entered_at);
    await app.close();
  });

  it('WINDOW_CLOSED after the window', async () => {
    const { app } = make('missed-window');
    const res = await app.inject({ method: 'POST', url: `/events/${EVT}/enter`, headers: await signIn(app) });
    expect(errorBodySchema.parse(res.json()).code).toBe('WINDOW_CLOSED');
    await app.close();
  });
});

describe('status reveals no draw information before the draw', () => {
  it.each(['window-open', 'entered-waiting', 'drawing'])('%s', async (scenario) => {
    const { app } = make(scenario);
    const res = await app.inject({ method: 'GET', url: `/events/${EVT}/status`, headers: await signIn(app) });
    const s = statusSchema.parse(res.json());
    expect(s.waitlist_position).toBeUndefined();
    expect(s.seat_no).toBeUndefined();
    expect(s.hold_expires_at).toBeUndefined();
    expect(s.public_id).toBeUndefined();
    await app.close();
  });
});

describe('status after the draw', () => {
  const cases: [string, string][] = [
    ['won-hold', 'WON'],
    ['hold-expired', 'EXPIRED'],
    ['waitlisted', 'WAITLISTED'],
    ['lost', 'LOST'],
    ['claimed', 'CLAIMED'],
    ['missed-window', 'REGISTERED'],
  ];
  it.each(cases)('%s -> %s', async (scenario, state) => {
    const { app } = make(scenario);
    const res = await app.inject({ method: 'GET', url: `/events/${EVT}/status`, headers: await signIn(app) });
    const s = statusSchema.parse(res.json());
    expect(s.state).toBe(state);
    if (state === 'WON') expect(s.hold_expires_at).toBeDefined();
    if (state === 'WAITLISTED') expect(s.waitlist_position).toBeGreaterThan(0);
    if (state === 'CLAIMED') expect(s.seat_no && s.ticket_code).toBeTruthy();
    await app.close();
  });
});

describe('claim', () => {
  it('requires an Idempotency-Key', async () => {
    const { app } = make('won-hold');
    const res = await app.inject({ method: 'POST', url: `/events/${EVT}/claim`, headers: await signIn(app) });
    expect(res.statusCode).toBe(400);
    await app.close();
  });

  it('claims once; the same key replays the same ticket; a new key gets ALREADY_CLAIMED', async () => {
    const { app } = make('won-hold');
    const headers = { ...(await signIn(app)), 'Idempotency-Key': 'key-aaaaaaaa' };
    const first = await app.inject({ method: 'POST', url: `/events/${EVT}/claim`, headers });
    const a = claimResponseSchema.parse(first.json());
    const replay = claimResponseSchema.parse((await app.inject({ method: 'POST', url: `/events/${EVT}/claim`, headers })).json());
    expect(replay).toEqual(a);
    const other = await app.inject({ method: 'POST', url: `/events/${EVT}/claim`, headers: { ...headers, 'Idempotency-Key': 'key-bbbbbbbb' } });
    expect(errorBodySchema.parse(other.json()).code).toBe('ALREADY_CLAIMED');
    await app.close();
  });

  it('HOLD_EXPIRED / NOT_WINNER', async () => {
    const expired = make('hold-expired');
    const h1 = { ...(await signIn(expired.app)), 'Idempotency-Key': 'key-cccccccc' };
    const r1 = await expired.app.inject({ method: 'POST', url: `/events/${EVT}/claim`, headers: h1 });
    expect(errorBodySchema.parse(r1.json()).code).toBe('HOLD_EXPIRED');
    await expired.app.close();

    const wl = make('waitlisted');
    const h2 = { ...(await signIn(wl.app)), 'Idempotency-Key': 'key-dddddddd' };
    const r2 = await wl.app.inject({ method: 'POST', url: `/events/${EVT}/claim`, headers: h2 });
    expect(errorBodySchema.parse(r2.json()).code).toBe('NOT_WINNER');
    await wl.app.close();
  });
});

describe('failure injection', () => {
  it('429 carries Retry-After and details, then recovers', async () => {
    const { app } = make('enter-rate-limited');
    const headers = await signIn(app);
    const res = await app.inject({ method: 'POST', url: `/events/${EVT}/enter`, headers });
    expect(res.statusCode).toBe(429);
    expect(Number(res.headers['retry-after'])).toBeGreaterThan(0);
    const body = errorBodySchema.parse(res.json());
    expect(body.code).toBe('RATE_LIMITED');
    expect(body.details).toMatchObject({ scope: 'user' });
    expect(typeof body.details?.retry_after_ms).toBe('number');
    await app.close();
  });

  it('pow challenge: 403 with a valid challenge, solve it, retry the SAME request with headers', async () => {
    const { app, world } = make('enter-challenge-pow');
    const headers = await signIn(app);
    const first = await app.inject({ method: 'POST', url: `/events/${EVT}/enter`, headers });
    expect(first.statusCode).toBe(403);
    const body = errorBodySchema.parse(first.json());
    expect(body.code).toBe('CHALLENGE_REQUIRED');
    const challenge = challengeSchema.parse(body.details?.challenge);
    expect(challenge.type).toBe('pow');

    // wrong solution -> a fresh challenge
    const wrong = await app.inject({
      method: 'POST',
      url: `/events/${EVT}/enter`,
      headers: { ...headers, 'X-Challenge-Id': challenge.id, 'X-Challenge-Solution': 'abc' },
    });
    expect(wrong.statusCode).toBe(403);
    const retry = errorBodySchema.parse(wrong.json());
    const fresh = challengeSchema.parse(retry.details?.challenge);
    expect(fresh.id).not.toBe(challenge.id);

    // a real solve (against the fresh challenge) goes through
    const solution = solvePow(fresh.pow!.prefix, fresh.pow!.difficulty_bits);
    const ok = await app.inject({
      method: 'POST',
      url: `/events/${EVT}/enter`,
      headers: { ...headers, 'X-Challenge-Id': fresh.id, 'X-Challenge-Solution': solution },
    });
    expect(ok.statusCode).toBe(200);
    expect(world.faults.has('enter:challenge_pow')).toBe(false);
    await app.close();
  });

  it('captcha challenge accepts the mock token', async () => {
    const { app } = make('enter-challenge-captcha');
    const headers = await signIn(app);
    const first = errorBodySchema.parse((await app.inject({ method: 'POST', url: `/events/${EVT}/enter`, headers })).json());
    const challenge = challengeSchema.parse(first.details?.challenge);
    expect(challenge.type).toBe('captcha');
    const ok = await app.inject({
      method: 'POST',
      url: `/events/${EVT}/enter`,
      headers: { ...headers, 'X-Challenge-Id': challenge.id, 'X-Challenge-Solution': 'mock-captcha-ok' },
    });
    expect(ok.statusCode).toBe(200);
    await app.close();
  });

  it('POST /defence/challenge issues a schema-valid challenge', async () => {
    const { app } = make('window-open');
    const res = await app.inject({ method: 'POST', url: '/defence/challenge', headers: await signIn(app), payload: { event_id: EVT } });
    expect(challengeSchema.safeParse(res.json()).success).toBe(true);
    await app.close();
  });

  it('REJECTED is a 403 with the standard body', async () => {
    const { app } = make('enter-rejected');
    const res = await app.inject({ method: 'POST', url: `/events/${EVT}/enter`, headers: await signIn(app) });
    expect(res.statusCode).toBe(403);
    expect(errorBodySchema.parse(res.json()).code).toBe('REJECTED');
    await app.close();
  });
});

describe('controls', () => {
  it('time travel changes the phase and server_now', async () => {
    const { app } = make('fresh');
    const before = eventSchema.parse((await app.inject({ method: 'GET', url: `/events/${EVT}` })).json());
    expect(before.phase).toBe('SCHEDULED');
    const state = (await app.inject({ method: 'POST', url: '/__mock/clock', payload: { set: '2026-11-01T10:05:00.000Z' } })).json();
    expect(state.server_now.startsWith('2026-11-01T10:05:00')).toBe(true);
    const after = eventSchema.parse((await app.inject({ method: 'GET', url: `/events/${EVT}` })).json());
    expect(after.phase).toBe('OPEN');
    await app.close();
  });

  it('unknown event is NOT_FOUND', async () => {
    const { app } = make('fresh');
    const res = await app.inject({ method: 'GET', url: '/events/nope' });
    expect(res.statusCode).toBe(404);
    expect(errorBodySchema.parse(res.json()).code).toBe('NOT_FOUND');
    await app.close();
  });
});

describe('pow helper', () => {
  it('counts leading zero bits', () => {
    expect(leadingZeroBits(Buffer.from([0x00, 0x0f]))).toBe(12);
    expect(leadingZeroBits(Buffer.from([0x80]))).toBe(0);
    expect(leadingZeroBits(Buffer.from([0x00, 0x00]))).toBe(16);
  });

  it('solvePow output really satisfies the stated rule', () => {
    const nonce = solvePow('prefix', 12);
    const digest = createHash('sha256').update(`prefix:${nonce}`).digest();
    expect(leadingZeroBits(digest)).toBeGreaterThanOrEqual(12);
  });
});
