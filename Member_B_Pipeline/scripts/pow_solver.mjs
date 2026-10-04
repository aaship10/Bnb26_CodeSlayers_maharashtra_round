/**
 * Reference proof-of-work solver in JavaScript (Node >= 18, no dependencies). For Member D and anyone
 * porting the solver. Member D's optimised browser solver (src/features/challenge/pow.ts) must agree with this.
 *
 *   node scripts/pow_solver.mjs <challenge-id-or-prefix> <difficulty_bits>
 *
 * Rule (docs/POW_SPEC.md): smallest decimal nonce n >= 0 such that SHA-256(utf8(prefix + ":" + n))
 * has at least `bits` leading zero bits. Send X-Challenge-Id: <challenge id>, X-Challenge-Solution: <n>.
 * In a browser use crypto.subtle only as a fallback: it is async and ~50x slower per hash than a
 * hand-rolled SHA-256 in a worker, which is why D's solver has its own implementation (midstate caching).
 */
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';

export function leadingZeroBits(digest) {
  let n = 0;
  for (const byte of digest) {
    if (byte === 0) {
      n += 8;
      continue;
    }
    return n + Math.clz32(byte) - 24; // clz32 counts in 32 bits; the byte occupies the low 8
  }
  return n;
}

export function solve(prefix, bits, start = 0, limit = 2 ** 40) {
  for (let n = start; n < limit; n++) {
    if (leadingZeroBits(createHash('sha256').update(`${prefix}:${n}`).digest()) >= bits) return String(n);
  }
  throw new Error('no solution within limit');
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const [prefix, bits] = process.argv.slice(2);
  if (!prefix || bits === undefined) {
    console.error('usage: node scripts/pow_solver.mjs <prefix> <bits>');
    process.exit(2);
  }
  const t = Date.now();
  const nonce = solve(prefix, Number(bits));
  console.log(`nonce=${nonce}  (${Number(nonce) + 1} attempts, ${((Date.now() - t) / 1000).toFixed(3)}s)`);
}
