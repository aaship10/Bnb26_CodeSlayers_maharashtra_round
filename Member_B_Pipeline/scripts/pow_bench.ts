/**
 * Benchmarks Member D's REAL browser solver (src/features/challenge/pow.ts, run here under Node/V8) and
 * the Python reference, on a challenge-shaped 128-character prefix.
 *
 *   D:\BnB_26\frontend\node_modules\.bin\tsx.cmd scripts/pow_bench.ts
 *
 * What this is NOT: a phone measurement. Numbers are for the machine you run it on. D's /__dev/pow-bench
 * page measures the same thing inside a real browser (run it on a phone to see the real cost there).
 */
import { solvePowSync } from '../../frontend/src/features/challenge/pow';

const prefix = 'fd1.p0.' + '1'.repeat(32) + '.' + '2'.repeat(32) + '.2000000000.14.0123456789abcdef.' + 'a'.repeat(24);
const runs = 7;

function median(xs: number[]): number {
  return [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)]!;
}

console.log(`prefix length ${prefix.length}; ${runs} different challenges per difficulty, median reported\n`);
console.log('bits  expected-hashes  median-ms   attempts/s (median)');
for (const bits of [14, 16, 18, 20, 22]) {
  const ms: number[] = [];
  const rate: number[] = [];
  for (let i = 0; i < runs; i++) {
    const p = prefix.replace('0123456789abcdef', (0x1000000000000000 + i * 7919 + bits).toString(16).slice(0, 16));
    const t = performance.now();
    const { hashes } = solvePowSync(p, bits);
    const dt = performance.now() - t;
    ms.push(dt);
    rate.push(hashes / (dt / 1000));
  }
  console.log(`${String(bits).padStart(4)}  ${String(2 ** bits).padStart(15)}  ${median(ms).toFixed(1).padStart(9)}   ${Math.round(median(rate)).toLocaleString()}`);
}
