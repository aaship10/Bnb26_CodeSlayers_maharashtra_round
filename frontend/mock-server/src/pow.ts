import { createHash } from 'node:crypto';

/**
 * PoW rule from the shared conventions: find a decimal-string nonce such that
 * SHA-256(utf8(prefix + ":" + nonce)) has at least difficulty_bits leading zero bits.
 * (B's docs/POW_SPEC.md is authoritative once it lands.)
 */
export function leadingZeroBits(buf: Buffer): number {
  let bits = 0;
  for (const byte of buf) {
    if (byte === 0) {
      bits += 8;
      continue;
    }
    bits += Math.clz32(byte) - 24;
    break;
  }
  return bits;
}

export function verifyPow(prefix: string, nonce: string, difficultyBits: number): boolean {
  if (!/^\d{1,20}$/.test(nonce)) return false;
  const digest = createHash('sha256').update(`${prefix}:${nonce}`, 'utf8').digest();
  return leadingZeroBits(digest) >= difficultyBits;
}

/** Brute-force solver, used by tests and never by the mock itself. */
export function solvePow(prefix: string, difficultyBits: number): string {
  for (let n = 0; ; n++) {
    if (verifyPow(prefix, String(n), difficultyBits)) return String(n);
  }
}
