import { createHash } from 'node:crypto';

export type Phase = 'DRAFT' | 'SCHEDULED' | 'OPEN' | 'DRAWING' | 'CLAIMING' | 'CLOSED';

export interface EventDef {
  id: string;
  name: string;
  description: string;
  inventory: number;
  mode: 'LOTTERY' | 'FCFS';
  opens_at: string;
  closes_at: string;
  claim_ttl_s: number;
}

/** How long the draw "runs" after the window closes, in mock time. */
export const DRAWING_MS = 10_000;

/** The clock every scenario starts from (fixed, so runs are reproducible). */
export const INITIAL_CLOCK = '2026-11-01T09:45:00.000Z';

export const PRIMARY_EVENT_ID = 'evt_demo_01';

export const EVENTS: EventDef[] = [
  {
    id: 'evt_demo_01',
    name: 'Moonlight Rooftop Sessions',
    description: 'One night, 500 seats, a skyline and a very good sound system.',
    inventory: 500,
    mode: 'LOTTERY',
    opens_at: '2026-11-01T10:00:00.000Z',
    closes_at: '2026-11-01T10:30:00.000Z',
    claim_ttl_s: 600,
  },
  {
    id: 'evt_demo_02',
    name: 'Static Bloom Festival',
    description: 'Three stages, one field, and a lineup announced the minute the draw ends.',
    inventory: 800,
    mode: 'LOTTERY',
    opens_at: '2026-11-15T10:00:00.000Z',
    closes_at: '2026-11-15T12:00:00.000Z',
    claim_ttl_s: 900,
  },
  {
    id: 'evt_demo_00',
    name: 'Paper Lanterns Night Market',
    description: "Last month's drop. Every seat was allocated by the draw.",
    inventory: 200,
    mode: 'LOTTERY',
    opens_at: '2026-10-01T10:00:00.000Z',
    closes_at: '2026-10-01T10:30:00.000Z',
    claim_ttl_s: 600,
  },
];

export function findEvent(id: string): EventDef | undefined {
  return EVENTS.find((e) => e.id === id);
}

export interface Timeline {
  opens: number;
  closes: number;
  drawEnds: number;
  holdEnds: number;
}

export function timeline(ev: EventDef): Timeline {
  const opens = Date.parse(ev.opens_at);
  const closes = Date.parse(ev.closes_at);
  const drawEnds = closes + DRAWING_MS;
  return { opens, closes, drawEnds, holdEnds: drawEnds + ev.claim_ttl_s * 1000 };
}

export function derivePhase(ev: EventDef, nowMs: number): Phase {
  const t = timeline(ev);
  if (nowMs < t.opens) return 'SCHEDULED';
  if (nowMs < t.closes) return 'OPEN';
  if (nowMs < t.drawEnds) return 'DRAWING';
  if (nowMs < t.holdEnds) return 'CLAIMING';
  return 'CLOSED';
}

/** Deterministic pseudo-data derived from stable inputs, so the mock never uses Math.random. */
export function hashHex(...parts: string[]): string {
  return createHash('sha256').update(parts.join('|')).digest('hex');
}

export function hashInt(...parts: string[]): number {
  return parseInt(hashHex(...parts).slice(0, 8), 16);
}

export function uuidFrom(input: string): string {
  const h = hashHex('uuid', input);
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-5${h.slice(13, 16)}-a${h.slice(17, 20)}-${h.slice(20, 32)}`;
}

const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
export function ticketCode(userId: string, eventId: string): string {
  const h = hashHex('ticket', userId, eventId);
  let out = '';
  for (let i = 0; i < 8; i++) out += CROCKFORD[parseInt(h.slice(i * 2, i * 2 + 2), 16) % 32];
  return `FD-${out.slice(0, 4)}-${out.slice(4)}`;
}
