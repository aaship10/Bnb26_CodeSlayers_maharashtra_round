import { createHash, createHmac } from 'node:crypto';

/**
 * The mock's own implementation of the PROVISIONAL draw (Node crypto, written
 * separately from the browser verifier). contract.test.ts checks it against the
 * Python reference (tools/ref_draw.py) so all three agree.
 */
export interface Entrant {
  public_id: string;
  weight: number;
}

const WEIGHT_TEXT: Record<string, string> = { '1': '1', '0.5': '0.5', '0.25': '0.25' };
const sha = (b: Buffer | string) => createHash('sha256').update(b).digest();

export function entrantsText(entrants: Entrant[]): string {
  return [...entrants]
    .sort((a, b) => (a.public_id < b.public_id ? -1 : a.public_id > b.public_id ? 1 : 0))
    .map((e) => `${e.public_id},${WEIGHT_TEXT[String(e.weight)]}`)
    .join('\n');
}

function lnBig(x: bigint): number {
  const bits = x.toString(2).length;
  if (bits <= 53) return Math.log(Number(x));
  const s = bits - 53;
  return Math.log(Number(x >> BigInt(s))) + s * Math.log(2);
}
const LN_D = lnBig(2n ** 256n + 1n);

export interface DrawResult {
  commitment: string;
  entrants_hash: string;
  final_seed: string;
  order: string[];
  winners: string[];
  waitlist: string[];
  winners_hash: string;
  waitlist_hash: string;
}

export function runDraw(eventId: string, serverSeed: Buffer, beacon: Buffer, entrants: Entrant[], inventory: number): DrawResult {
  const eHash = sha(entrantsText(entrants));
  const seed = sha(Buffer.concat([serverSeed, beacon, Buffer.from(eventId, 'utf8'), eHash]));
  const weighted = entrants.some((e) => e.weight !== 1);
  const rows = entrants.map((e) => {
    const h = createHmac('sha256', seed).update(e.public_id, 'utf8').digest();
    const n = BigInt(`0x${h.toString('hex')}`);
    return { id: e.public_id, key: weighted ? (LN_D - lnBig(n + 1n)) / e.weight : n };
  });
  rows.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const order = rows.map((r) => r.id);
  const winners = order.slice(0, Math.min(inventory, order.length));
  const waitlist = order.slice(winners.length);
  return {
    commitment: sha(serverSeed).toString('hex'),
    entrants_hash: eHash.toString('hex'),
    final_seed: seed.toString('hex'),
    order,
    winners,
    waitlist,
    winners_hash: sha(winners.join('\n')).toString('hex'),
    waitlist_hash: sha(waitlist.join('\n')).toString('hex'),
  };
}

/* deterministic mock inputs (same rules as tools/ref_draw.py) */

export const mockServerSeed = (eventId: string) => sha(`fd-mock-server-seed:${eventId}`);

export function mockBeacon(eventId: string): { round: number; randomness: Buffer } {
  const round = 4_200_000 + (parseInt(sha(`fd-mock-beacon-round:${eventId}`).toString('hex').slice(0, 8), 16) % 100_000);
  return { round, randomness: sha(`fd-mock-beacon:${round}`) };
}

let demoCache: Entrant[] | null = null;
export function demoEntrants(): Entrant[] {
  if (!demoCache) {
    demoCache = Array.from({ length: 50_000 }, (_, i) => ({ public_id: `p_${sha(`fd-demo-entrant:${i}`).toString('hex').slice(0, 12)}`, weight: 1 }));
  }
  return demoCache;
}

/* ------------------------------------------------------------------ audit */

export const GENESIS = '0'.repeat(64);

export function canonicalJson(v: unknown): string {
  if (v === null) return 'null';
  if (typeof v === 'boolean' || typeof v === 'number') return JSON.stringify(v);
  if (typeof v === 'string') return JSON.stringify(v);
  if (Array.isArray(v)) return `[${v.map(canonicalJson).join(',')}]`;
  const o = v as Record<string, unknown>;
  const keys = Object.keys(o).sort((a, b) => {
    const pa = [...a];
    const pb = [...b];
    for (let i = 0; i < Math.min(pa.length, pb.length); i++) {
      const d = pa[i]!.codePointAt(0)! - pb[i]!.codePointAt(0)!;
      if (d) return d;
    }
    return pa.length - pb.length;
  });
  return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(o[k])}`).join(',')}}`;
}

export interface AuditRecord {
  seq: number;
  type: string;
  ts: string;
  payload: Record<string, unknown>;
  prev_hash: string;
  hash: string;
}

export function chain(records: { type: string; ts: string; payload: Record<string, unknown> }[]): AuditRecord[] {
  let prev = GENESIS;
  return records.map((r, i) => {
    const seq = i + 1;
    const hash = sha(`${prev}\n${canonicalJson({ seq, type: r.type, ts: r.ts, payload: r.payload })}`).toString('hex');
    const rec = { seq, type: r.type, ts: r.ts, payload: r.payload, prev_hash: prev, hash };
    prev = hash;
    return rec;
  });
}
