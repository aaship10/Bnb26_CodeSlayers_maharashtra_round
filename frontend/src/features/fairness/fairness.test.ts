import { createHmac } from 'node:crypto';
import vectors from './vectors.json';
import { canonicalJson, GENESIS_HASH, auditPreimage } from './auditSpec';
import { verifyChain } from './auditVerify';
import { fromHex, pureHmacFactory, pureJsCrypto, toHex, webCrypto, type DrawCrypto } from './cryptoImpl';
import { LN_D, lnBig, rankEntrants, split } from './drawEngine';
import { entrantsCanonicalText, finalSeedPreimage, weightText, type Entrant } from './drawSpec';
import type { AuditRecord, Fairness } from './schemas';
import { entrantListProblem, firstMismatch, verifyDraw } from './verifyDraw';
import { sha256, utf8 } from '@/features/challenge/sha256';

type DrawCase = (typeof vectors.draw)[number];
const IMPLS: DrawCrypto[] = [pureJsCrypto, webCrypto(globalThis.crypto.subtle)];

describe('provisional encodings', () => {
  it('weights have exactly one text form', () => {
    expect([1, 0.5, 0.25].map(weightText)).toEqual(['1', '0.5', '0.25']);
    expect(() => weightText(0.3)).toThrow();
    expect(() => weightText(2)).toThrow();
  });

  it('entrant text sorts by public_id, has no trailing newline, and is empty for no entrants', () => {
    expect(entrantsCanonicalText([{ public_id: 'p_b', weight: 1 }, { public_id: 'p_a', weight: 0.5 }])).toBe('p_a,0.5\np_b,1');
    expect(entrantsCanonicalText([])).toBe('');
  });
});

describe('lnBig (BigInt logarithm)', () => {
  it('is exact for small values and close for huge ones', () => {
    expect(lnBig(1n)).toBe(0);
    expect(lnBig(1000n)).toBeCloseTo(Math.log(1000), 12);
    expect(lnBig(2n ** 200n)).toBeCloseTo(200 * Math.LN2, 9);
    expect(LN_D).toBeCloseTo(256 * Math.LN2, 9);
  });
  it('rejects non-positive input', () => {
    expect(() => lnBig(0n)).toThrow();
  });
});

describe('pure-JS HMAC-SHA256', () => {
  it('matches Node for many key and message lengths (including keys longer than a block)', () => {
    for (const keyLen of [0, 1, 32, 63, 64, 65, 200]) {
      for (const msgLen of [0, 1, 13, 55, 56, 64, 100]) {
        const key = new Uint8Array(keyLen).map((_, i) => (i * 7 + 3) & 0xff);
        const msg = new Uint8Array(msgLen).map((_, i) => (i * 13 + 1) & 0xff);
        expect(toHex(pureHmacFactory(key)(msg)), `${keyLen}/${msgLen}`).toBe(createHmac('sha256', key).update(msg).digest('hex'));
      }
    }
  });
});

describe.each(vectors.draw.map((c) => [c.name, c] as const))('draw vector "%s" (vs Python reference)', (_name, c: DrawCase) => {
  const entrants = c.entrants as Entrant[];

  it('commitment, entrants hash and final seed', () => {
    const seed = fromHex(c.server_seed);
    expect(toHex(sha256(seed))).toBe(c.expected.commitment);
    const eh = sha256(utf8(entrantsCanonicalText(entrants)));
    expect(toHex(eh)).toBe(c.expected.entrants_hash);
    expect(toHex(sha256(finalSeedPreimage(seed, fromHex(c.beacon_randomness), c.event_id, eh)))).toBe(c.expected.final_seed);
  });

  it.each(IMPLS.map((i) => [i.name, i] as const))('ranking with %s HMAC reproduces the order exactly', async (_n, impl) => {
    const sign = await impl.hmacSigner(fromHex(c.expected.final_seed));
    const ranked = await rankEntrants(entrants, sign);
    const order = ranked.map((r) => r.public_id);
    expect(order.slice(0, c.expected.order.length)).toEqual(c.expected.order);
    if ('hmac' in c.expected) {
      for (const r of ranked) expect(r.h).toBe((c.expected.hmac as Record<string, string>)[r.public_id]);
    }
    const { winners, waitlist } = split(order, c.inventory);
    expect(winners).toHaveLength(c.expected.winners_count);
    expect(waitlist).toHaveLength(c.expected.waitlist_count);
    expect(toHex(sha256(utf8(winners.join('\n'))))).toBe(c.expected.winners_hash);
    expect(toHex(sha256(utf8(waitlist.join('\n'))))).toBe(c.expected.waitlist_hash);
  });
});

/* ------------------------------------------------------------- verifyDraw */

function fairnessFor(c: DrawCase, over: Partial<Fairness> = {}): Fairness {
  return {
    event_id: c.event_id,
    phase: 'CLAIMING',
    algorithm_version: 'fd-draw/1-provisional',
    seed_commitment: c.expected.commitment,
    server_seed: c.server_seed,
    beacon: { source: 'test', round: 7, randomness: c.beacon_randomness },
    entrants_hash: c.expected.entrants_hash,
    entrants_count: c.entrants.length,
    final_seed: c.expected.final_seed,
    inventory: c.inventory,
    result: {
      winners_count: c.expected.winners_count,
      waitlist_count: c.expected.waitlist_count,
      winners_hash: c.expected.winners_hash,
      waitlist_hash: c.expected.waitlist_hash,
    },
    audit_head_hash: null,
    server_now: '2026-11-01T10:40:00.000Z',
    ...over,
  };
}

const sortedEntrants = (c: DrawCase) => [...(c.entrants as Entrant[])].sort((a, b) => (a.public_id < b.public_id ? -1 : 1));

async function run(c: DrawCase, over: Partial<Fairness> = {}, entrants = sortedEntrants(c), results: { winners: string[]; waitlist: string[] } | null = null) {
  return verifyDraw(
    { eventId: c.event_id, fairness: fairnessFor(c, over), loadEntrants: async () => entrants, loadResults: async () => (results ? { event_id: c.event_id, ...results } : null) },
    pureJsCrypto,
  );
}

describe('verifyDraw', () => {
  const [A, B, C, D, E] = vectors.draw as DrawCase[];

  it.each([A, B, C, D, E].map((c) => [c!.name, c!] as const))('passes all five steps on honest data: %s', async (_n, c) => {
    const out = await run(c);
    expect(out.verdict).toBe('verified');
    expect(out.steps.map((s) => s.status)).toEqual(['pass', 'pass', 'pass', 'pass', 'pass']);
  });

  it('checks published lists entry by entry when they exist', async () => {
    const order = A!.expected.order;
    const out = await run(A!, {}, sortedEntrants(A!), { winners: order.slice(0, 3), waitlist: order.slice(3) });
    expect(out.verdict).toBe('verified');
    expect(out.steps[4]!.detail).toMatch(/entry by entry/);
  });

  it('a seed that does not match the commitment fails step 1 and stops', async () => {
    const out = await run(A!, { server_seed: 'f'.repeat(64) });
    expect(out.verdict).toBe('failed');
    expect(out.steps[0]).toMatchObject({ status: 'fail' });
    expect(out.steps[0]!.detail).toMatch(/changed after the fact/);
    expect(out.steps.slice(1).every((s) => s.status === 'skipped')).toBe(true);
  });

  it('a TAMPERED entrant list fails step 2 with a clear message', async () => {
    const tampered = sortedEntrants(A!).map((e, i) => (i === 3 ? { ...e, public_id: e.public_id.slice(0, -1) + (e.public_id.endsWith('0') ? '1' : '0') } : e));
    tampered.sort((a, b) => (a.public_id < b.public_id ? -1 : 1));
    const out = await run(A!, {}, tampered);
    expect(out.steps[1]!.status).toBe('fail');
    expect(out.steps[1]!.detail).toMatch(/has changed since the window closed/);
  });

  it('an entry quietly dropped from the list is caught', async () => {
    const out = await run(A!, {}, sortedEntrants(A!).slice(1));
    expect(out.steps[1]!.status).toBe('fail');
  });

  it('a list that is not canonical (duplicates, bad weight, unsorted) is rejected before hashing', () => {
    expect(entrantListProblem([{ public_id: 'p_a', weight: 1 }, { public_id: 'p_a', weight: 1 }])).toMatch(/twice/);
    expect(entrantListProblem([{ public_id: 'p_b', weight: 1 }, { public_id: 'p_a', weight: 1 }])).toMatch(/canonical order/);
    expect(entrantListProblem([{ public_id: 'p_a', weight: 0.3 }])).toMatch(/weight/);
    expect(entrantListProblem([{ public_id: 'has,comma', weight: 1 }])).toMatch(/id/);
  });

  it('a wrong final seed fails step 3', async () => {
    const out = await run(A!, { final_seed: '0'.repeat(64) });
    expect(out.steps[2]!.status).toBe('fail');
  });

  it('a swapped winner fails step 5 and names the first difference', async () => {
    const order = [...A!.expected.order];
    const pubWinners = [order[0]!, order[3]!, order[2]!];
    const out = await run(
      A!,
      { result: { winners_count: 3, waitlist_count: 7, winners_hash: toHex(sha256(utf8(pubWinners.join('\n')))), waitlist_hash: A!.expected.waitlist_hash } },
      sortedEntrants(A!),
      { winners: pubWinners, waitlist: order.slice(3) },
    );
    expect(out.verdict).toBe('failed');
    expect(out.steps[4]!.detail).toMatch(/First difference at winner #2/);
  });

  it('before the draw: commitment shown, later steps skipped (never faked)', async () => {
    const out = await run(A!, { server_seed: null, final_seed: null, result: null, beacon: { source: 's', round: 9, randomness: null }, entrants_hash: null, entrants_count: null });
    expect(out.verdict).toBe('not_yet');
    expect(out.steps.map((s) => s.status)).toEqual(['skipped', 'skipped', 'skipped', 'skipped', 'skipped']);
  });

  it('after close but before the draw: the entrant list can already be checked', async () => {
    const out = await run(A!, { server_seed: null, final_seed: null, result: null, beacon: { source: 's', round: 9, randomness: null } });
    expect(out.verdict).toBe('not_yet');
    expect(out.steps.map((s) => s.status)).toEqual(['skipped', 'pass', 'skipped', 'skipped', 'skipped']);
  });

  it('firstMismatch', () => {
    expect(firstMismatch(['a', 'b'], ['a', 'b'])).toBe(-1);
    expect(firstMismatch(['a', 'b'], ['a', 'c'])).toBe(1);
    expect(firstMismatch(['a'], ['a', 'b'])).toBe(1);
  });
});

/* -------------------------------------------------------------- audit chain */

const records = vectors.audit.records as (AuditRecord & { canonical: string })[];
const subtleSha = async (b: Uint8Array) => new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', b.slice()));

describe('audit chain (vs Python reference)', () => {
  it('canonical JSON matches byte for byte, including unicode, escapes and key order', () => {
    for (const r of records) expect(canonicalJson({ seq: r.seq, type: r.type, ts: r.ts, payload: r.payload })).toBe(r.canonical);
  });

  it('each record hashes to the reference hash', () => {
    let prev = GENESIS_HASH;
    for (const r of records) {
      expect(r.prev_hash).toBe(prev);
      expect(toHex(sha256(auditPreimage(prev, r)))).toBe(r.hash);
      prev = r.hash;
    }
  });

  it('refuses floats rather than guessing their text form', () => {
    expect(() => canonicalJson({ x: 1.5 })).toThrow(/safe integers/);
  });

  it('verifies an intact chain', async () => {
    const out = await verifyChain(records, subtleSha);
    expect(out).toMatchObject({ ok: true, checked: 5, headSeq: 5, headHash: records[4]!.hash, firstBadSeq: null });
  });

  it('pinpoints an altered record, and only that one', async () => {
    const tampered = records.map((r) => (r.seq === 3 ? { ...r, payload: { ...(r.payload as object), note: 'edited' } } : r));
    const out = await verifyChain(tampered, subtleSha);
    expect(out.ok).toBe(false);
    expect(out.firstBadSeq).toBe(3);
    expect(out.records.filter((c) => !c.ok).map((c) => c.seq)).toEqual([3]);
    expect(out.records.find((c) => c.seq === 3)!.reason).toMatch(/altered/);
  });

  it('notices a removed record and a re-pointed link', async () => {
    const missing = await verifyChain(records.filter((r) => r.seq !== 2), subtleSha);
    expect(missing.firstBadSeq).toBe(3);
    const relinked = await verifyChain(records.map((r) => (r.seq === 4 ? { ...r, prev_hash: '1'.repeat(64) } : r)), subtleSha);
    expect(relinked.records.find((c) => c.seq === 4)!.reason).toMatch(/previous record/);
  });
});
