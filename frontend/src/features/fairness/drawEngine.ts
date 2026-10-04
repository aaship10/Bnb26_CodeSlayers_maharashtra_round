import { comparePublicId, rankMessage, type Entrant } from './drawSpec';
import { toHex } from './cryptoImpl';

const LN2 = Math.LN2;

/**
 * ln(x) for a BigInt of any size without floating-point overflow: keep the top
 * 53 bits as an exact double and add (bitlength - 53) * ln 2. Python's reference
 * does exactly the same operations in the same order.
 */
export function lnBig(x: bigint): number {
  if (x <= 0n) throw new RangeError('lnBig needs a positive value');
  const hex = x.toString(16);
  const lead = parseInt(hex[0]!, 16);
  const bits = (hex.length - 1) * 4 + (32 - Math.clz32(lead));
  if (bits <= 53) return Math.log(Number(x));
  const shift = bits - 53;
  return Math.log(Number(x >> BigInt(shift))) + shift * LN2;
}

/** ln(2^256 + 1), the denominator of u = (h + 1) / (2^256 + 1). */
export const LN_D = lnBig(2n ** 256n + 1n);

export interface Ranked {
  public_id: string;
  weight: number;
  /** HMAC as hex, kept for display and debugging. */
  h: string;
  /** Unweighted: the HMAC as a 256-bit integer. Weighted: -ln(u) / weight. Lower ranks first. */
  key: bigint | number;
}

export function isWeighted(entrants: readonly Entrant[]): boolean {
  return entrants.some((e) => e.weight !== 1);
}

/** Exponential-race key: -ln(u)/w with u = (h+1)/(2^256+1). Same op order as the reference: (lnD - ln(h+1)) / w. */
export function weightedKey(h: bigint, weight: number): number {
  return (LN_D - lnBig(h + 1n)) / weight;
}

function compareRanked(a: Ranked, b: Ranked): number {
  if (a.key < b.key) return -1;
  if (a.key > b.key) return 1;
  return comparePublicId(a.public_id, b.public_id); // tie-break
}

/**
 * Rank every entrant by HMAC(final_seed, public_id).
 * All weights 1: sort by the HMAC as a 256-bit integer (BigInt, no floating point).
 * Otherwise: the weighted exponential race with keys -ln(u)/weight.
 * Ties (astronomically unlikely) break by public_id.
 */
export async function rankEntrants(
  entrants: readonly Entrant[],
  sign: (msg: Uint8Array) => Promise<Uint8Array>,
  onProgress?: (fraction: number) => void,
): Promise<Ranked[]> {
  const weighted = isWeighted(entrants);
  const out: Ranked[] = new Array(entrants.length);
  const CHUNK = 2_000;
  for (let start = 0; start < entrants.length; start += CHUNK) {
    const slice = entrants.slice(start, start + CHUNK);
    const macs = await Promise.all(slice.map((e) => sign(rankMessage(e.public_id))));
    for (let i = 0; i < slice.length; i++) {
      const e = slice[i]!;
      const hex = toHex(macs[i]!);
      const n = BigInt(`0x${hex}`);
      out[start + i] = { public_id: e.public_id, weight: e.weight, h: hex, key: weighted ? weightedKey(n, e.weight) : n };
    }
    onProgress?.(Math.min(1, (start + slice.length) / Math.max(1, entrants.length)));
  }
  return out.sort(compareRanked);
}

/** First N win; everyone else is waitlisted in rank order. Handles zero entrants and fewer entrants than seats. */
export function split(order: readonly string[], inventory: number): { winners: string[]; waitlist: string[] } {
  const n = Math.max(0, Math.min(inventory, order.length));
  return { winners: order.slice(0, n), waitlist: order.slice(n) };
}
