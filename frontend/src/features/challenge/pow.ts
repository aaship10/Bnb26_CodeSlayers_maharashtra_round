import { IV, compress, loadBlock, utf8 } from './sha256';

/**
 * Proof-of-work search (shared conventions, section 3):
 *   find a decimal-string nonce such that SHA-256(utf8(prefix + ":" + nonce))
 *   has at least `difficultyBits` leading zero bits.
 *
 * Tuned for the hot loop:
 *  - the prefix is hashed once; only the blocks that contain the nonce digits
 *    are recomputed per attempt (midstate);
 *  - the nonce is a decimal string that is incremented in place in the message
 *    buffer, so there is no string building or allocation per attempt;
 *  - padding is re-laid-out only when the digit count changes (9 -> 10 ...).
 *
 * pow.test.ts checks every decision against Node's crypto and against vectors
 * from an independent Python implementation.
 */

const ZERO = 0x30;
const NINE = 0x39;
const TWO_32 = 0x100000000;

/** True when the first `bits` bits of the digest (as 8 big-endian words) are all zero. */
export function hasLeadingZeroBits(h: Int32Array, bits: number): boolean {
  let remaining = bits;
  for (let i = 0; i < 8 && remaining > 0; i++) {
    const v = h[i]!;
    if (remaining >= 32) {
      if (v !== 0) return false;
      remaining -= 32;
    } else {
      return Math.clz32(v) >= remaining;
    }
  }
  return true;
}

export class PowSearcher {
  private readonly mid = new Int32Array(IV);
  private readonly h = new Int32Array(8);
  private readonly w = new Int32Array(64);
  private readonly buf = new Uint8Array(128);
  private readonly tailLen: number;
  private readonly prefixLen: number; // bytes of prefix + ":"
  private dlen = 0;
  private blocks = 1;

  constructor(
    prefix: string,
    private readonly difficultyBits: number,
  ) {
    const msg = utf8(`${prefix}:`);
    this.prefixLen = msg.length;
    const full = Math.floor(msg.length / 64);
    const scratch = new Int32Array(64);
    for (let i = 0; i < full; i++) {
      loadBlock(msg, i * 64, scratch);
      compress(this.mid, scratch);
    }
    const tail = msg.subarray(full * 64);
    this.tailLen = tail.length;
    this.buf.set(tail);
  }

  /** Lay out padding and length for a message with `dlen` nonce digits. */
  private layout(dlen: number): void {
    this.dlen = dlen;
    const msgLen = this.tailLen + dlen;
    this.blocks = msgLen + 9 <= 64 ? 1 : 2;
    const end = this.blocks * 64;
    this.buf.fill(0, msgLen, 128);
    this.buf[msgLen] = 0x80;
    const bits = (this.prefixLen + dlen) * 8;
    const hi = Math.floor(bits / TWO_32);
    const lo = bits >>> 0;
    this.buf[end - 8] = hi >>> 24;
    this.buf[end - 7] = (hi >>> 16) & 0xff;
    this.buf[end - 6] = (hi >>> 8) & 0xff;
    this.buf[end - 5] = hi & 0xff;
    this.buf[end - 4] = lo >>> 24;
    this.buf[end - 3] = (lo >>> 16) & 0xff;
    this.buf[end - 2] = (lo >>> 8) & 0xff;
    this.buf[end - 1] = lo & 0xff;
  }

  /** Add one to the decimal string in the buffer. */
  private increment(): void {
    const first = this.tailLen;
    let i = first + this.dlen - 1;
    while (i >= first) {
      if (this.buf[i] === NINE) {
        this.buf[i] = ZERO;
        i--;
      } else {
        this.buf[i] = this.buf[i]! + 1;
        return;
      }
    }
    // 99..9 rolled over to 00..0: grow to 100..0
    const dlen = this.dlen + 1;
    this.buf[first] = ZERO + 1;
    for (let k = 1; k < dlen; k++) this.buf[first + k] = ZERO;
    this.layout(dlen);
  }

  /**
   * Try nonces start, start+1, ... start+count-1.
   * Returns the first nonce that satisfies the difficulty, or null.
   */
  search(start: number, count: number): number | null {
    const digits = String(start);
    for (let i = 0; i < digits.length; i++) this.buf[this.tailLen + i] = digits.charCodeAt(i);
    this.layout(digits.length);

    const { buf, h, w, mid, difficultyBits } = this;
    for (let n = start, last = start + count; n < last; n++) {
      h.set(mid);
      loadBlock(buf, 0, w);
      compress(h, w);
      if (this.blocks === 2) {
        loadBlock(buf, 64, w);
        compress(h, w);
      }
      if (hasLeadingZeroBits(h, difficultyBits)) return n;
      this.increment();
    }
    return null;
  }
}

export interface SolveOptions {
  /** Called with the total attempts so far, roughly every `progressEvery` attempts. */
  onProgress?: (hashes: number) => void;
  progressEvery?: number;
  /** Give up (throw) after this many attempts. Guards against an absurd difficulty. */
  maxHashes?: number;
}

export interface SolveResult {
  nonce: string;
  hashes: number;
}

/** Synchronous solver for a worker (or tests). Searches 0, 1, 2, ... so the answer is the smallest valid nonce. */
export function solvePowSync(prefix: string, difficultyBits: number, opts: SolveOptions = {}): SolveResult {
  const { onProgress, progressEvery = 50_000, maxHashes = 2 ** 36 } = opts;
  const searcher = new PowSearcher(prefix, difficultyBits);
  let start = 0;
  while (start < maxHashes) {
    const found = searcher.search(start, progressEvery);
    if (found !== null) return { nonce: String(found), hashes: found + 1 };
    start += progressEvery;
    onProgress?.(start);
  }
  throw new Error(`proof-of-work not found within ${maxHashes} attempts`);
}

/** Resumable chunk search, for the main-thread fallback that yields between chunks. */
export function createChunkedSolver(prefix: string, difficultyBits: number) {
  const searcher = new PowSearcher(prefix, difficultyBits);
  let next = 0;
  return {
    /** Search the next `count` nonces. Returns the nonce if found. */
    step(count: number): string | null {
      const found = searcher.search(next, count);
      if (found !== null) return String(found);
      next += count;
      return null;
    },
    get attempts(): number {
      return next;
    },
  };
}
