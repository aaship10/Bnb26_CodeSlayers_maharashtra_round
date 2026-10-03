/**
 * ┌──────────────────────────────────────────────────────────────────────────┐
 * │ PROVISIONAL audit-chain encoding (A documents the real one).            │
 * │   hash = hex(SHA-256(utf8(prev_hash + "\n" + canonical_json(record))))   │
 * │   record = { seq, type, ts, payload }, first prev_hash = "0" x 64        │
 * │   canonical_json: object keys sorted by code point, no whitespace,       │
 * │   strings escaped as JSON.stringify does, integers only (no floats).     │
 * │ Isolated here so A's encoding replaces this file and nothing else.      │
 * └──────────────────────────────────────────────────────────────────────────┘
 */
import { utf8 } from '@/features/challenge/sha256';

export const GENESIS_HASH = '0'.repeat(64);

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

const byCodePoint = (a: string, b: string): number => {
  // Compare by Unicode code point (not UTF-16 unit), matching Python's sorted().
  const ia = [...a];
  const ib = [...b];
  for (let i = 0; i < Math.min(ia.length, ib.length); i++) {
    const d = ia[i]!.codePointAt(0)! - ib[i]!.codePointAt(0)!;
    if (d !== 0) return d;
  }
  return ia.length - ib.length;
};

export function canonicalJson(v: unknown): string {
  if (v === null) return 'null';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'number') {
    if (!Number.isSafeInteger(v)) throw new Error(`audit payloads may only contain safe integers, got ${v}`);
    return String(v);
  }
  if (typeof v === 'string') return JSON.stringify(v);
  if (Array.isArray(v)) return `[${v.map(canonicalJson).join(',')}]`;
  if (typeof v === 'object') {
    const obj = v as Record<string, unknown>;
    const keys = Object.keys(obj).sort(byCodePoint);
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(obj[k])}`).join(',')}}`;
  }
  throw new Error(`unsupported value in audit payload: ${typeof v}`);
}

export interface AuditRecordCore {
  seq: number;
  type: string;
  ts: string;
  /** Missing payloads fail canonicalisation loudly rather than hashing as something. */
  payload?: unknown;
}

/** The exact bytes hashed for one record. */
export function auditPreimage(prevHash: string, r: AuditRecordCore): Uint8Array {
  return utf8(`${prevHash}\n${canonicalJson({ seq: r.seq, type: r.type, ts: r.ts, payload: r.payload })}`);
}
