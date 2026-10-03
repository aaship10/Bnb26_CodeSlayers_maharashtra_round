import { toHex } from './cryptoImpl';
import { GENESIS_HASH, auditPreimage } from './auditSpec';
import type { AuditRecord } from './schemas';

export interface RecordCheck {
  seq: number;
  ok: boolean;
  /** Why this record breaks the chain. */
  reason?: string;
}

export interface ChainCheck {
  ok: boolean;
  checked: number;
  headSeq: number;
  headHash: string;
  firstBadSeq: number | null;
  records: RecordCheck[];
}

/**
 * Recompute the audit hash chain from the records alone. Each record must
 * (1) follow the previous seq, (2) point at the previous record's hash, and
 * (3) hash, under the documented encoding, to the hash it claims.
 */
export async function verifyChain(records: readonly AuditRecord[], sha256: (b: Uint8Array) => Promise<Uint8Array>): Promise<ChainCheck> {
  const checks: RecordCheck[] = [];
  let prev = GENESIS_HASH;
  let expectSeq = 1;
  let firstBad: number | null = null;

  for (const r of [...records].sort((a, b) => a.seq - b.seq)) {
    let reason: string | undefined;
    if (r.seq !== expectSeq) reason = `expected record #${expectSeq} here, found #${r.seq} (missing or reordered records)`;
    else if (r.prev_hash !== prev) reason = 'it doesn’t point at the previous record’s hash';
    else {
      let computed: string;
      try {
        computed = toHex(await sha256(auditPreimage(prev, r)));
      } catch (e) {
        computed = '';
        reason = `its payload can’t be encoded canonically (${e instanceof Error ? e.message : String(e)})`;
      }
      if (!reason && computed !== r.hash) reason = 'its contents don’t hash to the hash it claims (the record was altered)';
    }
    checks.push({ seq: r.seq, ok: !reason, reason });
    if (reason && firstBad === null) firstBad = r.seq;
    // Keep walking from the hash the record CLAIMS, so one bad record doesn't mark every later one bad.
    prev = r.hash;
    expectSeq = r.seq + 1;
  }

  const last = checks.length ? records.find((r) => r.seq === checks[checks.length - 1]!.seq)! : null;
  return {
    ok: firstBad === null,
    checked: checks.length,
    headSeq: last?.seq ?? 0,
    headHash: last?.hash ?? GENESIS_HASH,
    firstBadSeq: firstBad,
    records: checks,
  };
}
