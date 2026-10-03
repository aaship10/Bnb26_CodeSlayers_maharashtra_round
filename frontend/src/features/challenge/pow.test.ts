import { createHash } from 'node:crypto';
import { PowSearcher, createChunkedSolver, hasLeadingZeroBits, solvePowSync } from './pow';
import { POW_VECTORS } from './pow.vectors';
import { solveOnMainThread, solvePowInWorker } from './solvePow';

function leadingZeroBits(digest: Buffer): number {
  let bits = 0;
  for (const byte of digest) {
    if (byte === 0) {
      bits += 8;
      continue;
    }
    bits += Math.clz32(byte) - 24;
    break;
  }
  return bits;
}

const reference = (prefix: string, nonce: number | string) => createHash('sha256').update(`${prefix}:${nonce}`, 'utf8').digest();

describe('hasLeadingZeroBits', () => {
  it('checks across word boundaries', () => {
    const h = new Int32Array(8);
    expect(hasLeadingZeroBits(h, 256)).toBe(true);
    h[1] = 1 << 28; // 32 zero bits from word 0, then 3 from word 1
    expect(hasLeadingZeroBits(h, 32)).toBe(true);
    expect(hasLeadingZeroBits(h, 35)).toBe(true);
    expect(hasLeadingZeroBits(h, 36)).toBe(false);
    h[0] = 1;
    expect(hasLeadingZeroBits(h, 31)).toBe(true);
    expect(hasLeadingZeroBits(h, 32)).toBe(false);
  });

  it('treats zero difficulty as always satisfied', () => {
    expect(hasLeadingZeroBits(new Int32Array(8).fill(-1), 0)).toBe(true);
  });
});

describe('PoW solver vs independent Python vectors', () => {
  it('has the vectors', () => {
    expect(POW_VECTORS.length).toBeGreaterThan(20);
  });

  it.each(POW_VECTORS.map((v) => [v.prefix.length > 24 ? `${v.prefix.slice(0, 21)}... (${v.prefix.length}B)` : v.prefix, v.bits, v] as const))(
    '%s @ %d bits',
    (_label, _bits, v) => {
      expect(solvePowSync(v.prefix, v.bits).nonce).toBe(v.nonce);
    },
  );
});

describe('PoW searcher vs Node crypto, attempt by attempt', () => {
  // Prefix lengths chosen so prefix+":" lands on every awkward spot: well below 55,
  // exactly 55, 56, 63, 64, and multi-block.
  const lengths = [0, 1, 20, 49, 50, 54, 55, 56, 62, 63, 64, 65, 100, 130];

  it.each(lengths)('prefix length %d: accepts exactly the nonces Node accepts (including digit-count rollovers)', (len) => {
    const prefix = 'p'.repeat(len);
    const bits = 3; // ~1 in 8, so plenty of hits and misses over the range
    const range = 1300; // crosses 9->10, 99->100, 999->1000

    const expected: number[] = [];
    for (let n = 0; n < range; n++) if (leadingZeroBits(reference(prefix, n)) >= bits) expected.push(n);
    expect(expected.length).toBeGreaterThan(50);

    const searcher = new PowSearcher(prefix, bits);
    const got: number[] = [];
    let start = 0;
    while (start < range) {
      const found = searcher.search(start, range - start);
      if (found === null) break;
      got.push(found);
      start = found + 1;
    }
    expect(got).toEqual(expected);
  });

  it('works from a large starting nonce (digit buffer is rebuilt)', () => {
    const prefix = 'start-high';
    const start = 987_654_321;
    const bits = 4;
    const searcher = new PowSearcher(prefix, bits);
    const found = searcher.search(start, 5000)!;
    expect(found).toBeGreaterThanOrEqual(start);
    expect(leadingZeroBits(reference(prefix, found))).toBeGreaterThanOrEqual(bits);
    for (let n = start; n < found; n++) expect(leadingZeroBits(reference(prefix, n))).toBeLessThan(bits);
  });

  it('every nonce the solver returns satisfies the rule', () => {
    for (let i = 0; i < 25; i++) {
      const prefix = `fd1.evt.${i}.${'q'.repeat(i * 3)}`;
      const { nonce } = solvePowSync(prefix, 10);
      expect(nonce).toMatch(/^\d+$/);
      expect(leadingZeroBits(reference(prefix, nonce))).toBeGreaterThanOrEqual(10);
    }
  });
});

describe('solver plumbing', () => {
  it('reports progress and respects maxHashes', () => {
    const seen: number[] = [];
    expect(() => solvePowSync('never', 40, { progressEvery: 1000, maxHashes: 3500, onProgress: (h) => seen.push(h) })).toThrow(/not found/);
    expect(seen).toEqual([1000, 2000, 3000, 4000]);
  });

  it('chunked solver finds the same answer as the sync one', () => {
    const chunked = createChunkedSolver('vector-a', 12);
    let nonce: string | null = null;
    while (nonce === null) nonce = chunked.step(333);
    expect(nonce).toBe(solvePowSync('vector-a', 12).nonce);
  });

  it('main-thread fallback solves and can be cancelled', async () => {
    expect(await solveOnMainThread({ prefix: 'vector-a', difficultyBits: 12 })).toBe('1965');
    const ctrl = new AbortController();
    const p = solveOnMainThread({ prefix: 'never', difficultyBits: 60 }, { signal: ctrl.signal });
    setTimeout(() => ctrl.abort(), 20);
    await expect(p).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('solvePowInWorker falls back to the main thread when Workers are unavailable (node)', async () => {
    expect(typeof Worker).toBe('undefined');
    const progress: number[] = [];
    const nonce = await solvePowInWorker({ prefix: 'vector-a', difficultyBits: 16 }, { onProgress: (h) => progress.push(h) });
    expect(nonce).toBe(POW_VECTORS.find((v) => v.prefix === 'vector-a' && v.bits === 16)!.nonce);
    expect(progress.length).toBeGreaterThan(0);
  });

  it('rejects immediately if already aborted', async () => {
    const ctrl = new AbortController();
    ctrl.abort();
    await expect(solvePowInWorker({ prefix: 'x', difficultyBits: 8 }, { signal: ctrl.signal })).rejects.toMatchObject({ name: 'AbortError' });
  });
});

describe('throughput (informational)', () => {
  it('does at least 100k attempts/s even on a slow CI box', () => {
    const t0 = performance.now();
    const searcher = new PowSearcher('bench', 64); // unreachable difficulty
    searcher.search(0, 200_000);
    const rate = 200_000 / ((performance.now() - t0) / 1000);
    console.log(`PoW throughput in Node: ${Math.round(rate).toLocaleString()} attempts/s`);
    expect(rate).toBeGreaterThan(100_000);
  });
});
