/**
 * ┌──────────────────────────────────────────────────────────────────────────┐
 * │ PROVISIONAL ENCODINGS. Every byte-level choice of the draw lives here.  │
 * │ A owns the real spec (docs/DRAW_SPEC.md). Until it lands we follow       │
 * │ docs/DRAW_SPEC_PROVISIONAL.md, pinned by vectors from tools/ref_draw.py. │
 * │ When A's spec arrives: change ONLY this file, run A's vectors, and file  │
 * │ any mismatch as a bug report to A.                                       │
 * └──────────────────────────────────────────────────────────────────────────┘
 */
import { utf8 } from '@/features/challenge/sha256';

export const PROVISIONAL_ALGORITHM = 'fd-draw/1-provisional';

export interface Entrant {
  public_id: string;
  weight: number;
}

/** Allowed weights and their exact text form in the entrant list ("1", "0.5", "0.25"). */
const WEIGHT_TEXT = new Map<number, string>([
  [1, '1'],
  [0.5, '0.5'],
  [0.25, '0.25'],
]);

export function weightText(w: number): string {
  const t = WEIGHT_TEXT.get(w);
  if (t === undefined) throw new Error(`weight ${w} is not one of 1, 0.5, 0.25`);
  return t;
}

/** public_ids are compared by UTF-16 code unit (identical to code point order for the ASCII ids we accept). */
export const comparePublicId = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

/** Lines "public_id,weight" sorted ascending by public_id, joined by "\n", no trailing newline. */
export function entrantsCanonicalText(entrants: readonly Entrant[]): string {
  return [...entrants]
    .sort((a, b) => comparePublicId(a.public_id, b.public_id))
    .map((e) => `${e.public_id},${weightText(e.weight)}`)
    .join('\n');
}

/** final_seed preimage: server_seed (32 raw bytes) || beacon randomness (32 raw bytes) || utf8(event_id) || entrants_hash (32 raw bytes). */
export function finalSeedPreimage(serverSeed: Uint8Array, beacon: Uint8Array, eventId: string, entrantsHash: Uint8Array): Uint8Array {
  const id = utf8(eventId);
  const out = new Uint8Array(serverSeed.length + beacon.length + id.length + entrantsHash.length);
  let o = 0;
  for (const part of [serverSeed, beacon, id, entrantsHash]) {
    out.set(part, o);
    o += part.length;
  }
  return out;
}

/** HMAC message for one entrant. */
export function rankMessage(publicId: string): Uint8Array {
  return utf8(publicId);
}

/** Text hashed for the published winners_hash / waitlist_hash: ids in draw order joined by "\n". */
export function resultListText(ids: readonly string[]): string {
  return ids.join('\n');
}

/** Ids we accept: printable ASCII without commas or whitespace (so the list text is unambiguous). */
export const PUBLIC_ID_RE = /^[\x21-\x2b\x2d-\x7e]{1,128}$/;
