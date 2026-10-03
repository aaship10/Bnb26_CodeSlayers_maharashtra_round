/**
 * Fairness and audit contract: Python reference, the mock's Node implementation
 * and the browser verifier must all agree, and every tamper mode must be caught.
 */
import { buildApp } from './src/app';
import { runDraw } from './src/draw';
import demo from './fixtures/demo_draw.json';
import vectors from '../src/features/fairness/vectors.json';
import { eventSchema } from '../src/api/schemas';
import { auditPageSchema, auditVerifySchema, entrantsSchema, fairnessSchema, resultsSchema } from '../src/features/fairness/schemas';
import { verifyDraw } from '../src/features/fairness/verifyDraw';
import { verifyChain } from '../src/features/fairness/auditVerify';
import { pureJsCrypto } from '../src/features/fairness/cryptoImpl';

const EVT = 'evt_demo_01';
const sha = async (b: Uint8Array) => new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', b.slice()));

function make(scenario: string) {
  const m = buildApp();
  m.world.reset(scenario);
  return m;
}

const get = async (app: ReturnType<typeof buildApp>['app'], url: string) => (await app.inject({ method: 'GET', url })).json();

async function verifyAgainst(app: ReturnType<typeof buildApp>['app']) {
  const fairness = fairnessSchema.parse(await get(app, `/events/${EVT}/fairness`));
  return verifyDraw(
    {
      eventId: EVT,
      fairness,
      loadEntrants: async () => entrantsSchema.parse(await get(app, `/events/${EVT}/fairness/entrants`)).entrants,
      loadResults: async () => resultsSchema.parse(await get(app, `/events/${EVT}/fairness/results`)),
    },
    pureJsCrypto,
  );
}

describe('draw implementations agree', () => {
  it('the mock draw matches the Python reference on every vector', () => {
    for (const c of vectors.draw) {
      const r = runDraw(c.event_id, Buffer.from(c.server_seed, 'hex'), Buffer.from(c.beacon_randomness, 'hex'), c.entrants, c.inventory);
      expect(r.entrants_hash, c.name).toBe(c.expected.entrants_hash);
      expect(r.final_seed, c.name).toBe(c.expected.final_seed);
      expect(r.order.slice(0, c.expected.order.length), c.name).toEqual(c.expected.order);
      expect(r.winners_hash, c.name).toBe(c.expected.winners_hash);
      expect(r.waitlist_hash, c.name).toBe(c.expected.waitlist_hash);
    }
  });

  it('the 50,000-entrant demo draw served by the mock matches the Python fixture', async () => {
    const { app } = make('won-hold');
    const f = fairnessSchema.parse(await get(app, `/events/${EVT}/fairness`));
    expect(f).toMatchObject({
      seed_commitment: demo.commitment,
      server_seed: demo.server_seed,
      entrants_hash: demo.entrants_hash,
      entrants_count: 50_000,
      final_seed: demo.final_seed,
      synthetic: true,
    });
    expect(f.beacon).toEqual(demo.beacon);
    expect(f.result).toMatchObject({ winners_count: 500, waitlist_count: 49_500, winners_hash: demo.winners_hash, waitlist_hash: demo.waitlist_hash });
    const results = resultsSchema.parse(await get(app, `/events/${EVT}/fairness/results`));
    expect(results.winners.slice(0, 10)).toEqual(demo.first_winners);
    expect(results.waitlist.slice(0, 10)).toEqual(demo.first_waitlisted);
    expect(eventSchema.parse(await get(app, `/events/${EVT}`)).seed_commitment).toBe(demo.commitment);
    await app.close();
  }, 30_000);
});

describe('publication stages', () => {
  it('before the window: commitment and beacon round only', async () => {
    const { app } = make('window-open');
    const f = fairnessSchema.parse(await get(app, `/events/${EVT}/fairness`));
    expect(f).toMatchObject({ server_seed: null, entrants_hash: null, final_seed: null, result: null });
    expect(f.beacon?.randomness).toBeNull();
    expect((await app.inject({ method: 'GET', url: `/events/${EVT}/fairness/entrants` })).statusCode).toBe(409);
    await app.close();
  });

  it('after close, before the draw: the entrant list but no seed or results', async () => {
    const { app } = make('drawing');
    const f = fairnessSchema.parse(await get(app, `/events/${EVT}/fairness`));
    expect(f.entrants_hash).not.toBeNull();
    expect(f.server_seed).toBeNull();
    expect((await app.inject({ method: 'GET', url: `/events/${EVT}/fairness/results` })).statusCode).toBe(409);
    const out = await verifyAgainst(app);
    expect(out.verdict).toBe('not_yet');
    expect(out.steps[1]!.status).toBe('pass');
    await app.close();
  }, 30_000);
});

describe('the browser verifier against the mock', () => {
  it('verifies all 50,000 entries', async () => {
    const { app } = make('won-hold');
    const out = await verifyAgainst(app);
    expect(out.verdict).toBe('verified');
    expect(out.order).toHaveLength(50_000);
    expect(out.order!.slice(0, 10)).toEqual(demo.first_winners);
    await app.close();
  }, 60_000);

  it.each([
    ['seed', 0],
    ['entrants', 1],
    ['results', 4],
  ] as const)('tamper mode "%s" is caught at step %d', async (mode, stepIndex) => {
    const { app } = make('won-hold');
    await app.inject({ method: 'POST', url: '/__mock/tamper', payload: { mode } });
    const out = await verifyAgainst(app);
    expect(out.verdict).toBe('failed');
    expect(out.steps[stepIndex]!.status).toBe('fail');
    await app.close();
  }, 60_000);
});

describe('audit chain', () => {
  it('is paginated, verifies in the browser, and its head matches the fairness page and the server', async () => {
    const { app } = make('won-hold');
    const p1 = auditPageSchema.parse(await get(app, `/events/${EVT}/audit?from_seq=1&limit=4`));
    expect(p1.records).toHaveLength(4);
    expect(p1.next_from_seq).toBe(5);
    const p2 = auditPageSchema.parse(await get(app, `/events/${EVT}/audit?from_seq=5&limit=50`));
    expect(p2.next_from_seq).toBeNull();
    const check = await verifyChain([...p1.records, ...p2.records], sha);
    expect(check.ok).toBe(true);
    const fairness = fairnessSchema.parse(await get(app, `/events/${EVT}/fairness`));
    expect(check.headHash).toBe(fairness.audit_head_hash);
    expect(auditVerifySchema.parse(await get(app, `/events/${EVT}/audit/verify`)).head_hash).toBe(check.headHash);
    await app.close();
  });

  it('"audit" tampering is caught by the browser even though the server still claims ok', async () => {
    const { app } = make('won-hold');
    await app.inject({ method: 'POST', url: '/__mock/tamper', payload: { mode: 'audit' } });
    const records = auditPageSchema.parse(await get(app, `/events/${EVT}/audit?limit=200`)).records;
    const check = await verifyChain(records, sha);
    expect(check.ok).toBe(false);
    expect(check.firstBadSeq).toBe(2);
    expect(auditVerifySchema.parse(await get(app, `/events/${EVT}/audit/verify`)).ok).toBe(true);
    await app.close();
  });
});
