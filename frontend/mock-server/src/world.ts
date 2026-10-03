import { MockClock } from './clock';
import {
  EVENTS,
  INITIAL_CLOCK,
  PRIMARY_EVENT_ID,
  derivePhase,
  hashInt,
  hashHex,
  ticketCode,
  timeline,
  type EventDef,
  type Phase,
} from './events';
import { SCENARIOS } from './scenarios';
import { defaultDefences, type DefenceConfig } from './defences';

export const FAULT_TARGETS = ['enter', 'claim', 'status'] as const;
export const FAULT_KINDS = [
  'rate_limited',
  'challenge_pow',
  'challenge_captcha',
  'rejected',
  'network_fail',
  'slow',
  'server_error',
] as const;
export type FaultTarget = (typeof FAULT_TARGETS)[number];
export type FaultKind = (typeof FAULT_KINDS)[number];
export type FaultKey = `${FaultTarget}:${FaultKind}`;

export interface ArmedFault {
  key: FaultKey;
  /** network_fail / server_error disarm themselves after this many hits. */
  remaining?: number;
  /** rate_limited: real-time moment the block lifts. */
  blockedUntil?: number;
}

export interface User {
  id: string;
  email: string;
  display_name: string;
}

export interface Entry {
  entered_at: string;
  claim?: { seat_no: number; ticket_code: string };
}

export interface IssuedChallenge {
  id: string;
  type: 'pow' | 'captcha';
  prefix?: string;
  difficulty_bits?: number;
  expires_at: string;
}

export type MockStatus = {
  state: 'REGISTERED' | 'ENTERED' | 'WON' | 'WAITLISTED' | 'LOST' | 'CLAIMED' | 'EXPIRED';
  phase: Phase;
  hold_expires_at?: string;
  seat_no?: number;
  ticket_code?: string;
  waitlist_position?: number;
  public_id?: string;
};

export function parseFaultKey(key: string): FaultKey | null {
  const [t, k] = key.split(':');
  if ((FAULT_TARGETS as readonly string[]).includes(t ?? '') && (FAULT_KINDS as readonly string[]).includes(k ?? '')) {
    return key as FaultKey;
  }
  return null;
}

export const OTP = '123456';
export const POW_BITS = Number(process.env.MOCK_POW_BITS ?? 18);

/** All mutable mock state in one place so a scenario reset is a single call. */
export class World {
  readonly clock = new MockClock(INITIAL_CLOCK);
  scenarioId = 'fresh';
  users = new Map<string, User>();
  pending = new Map<string, string>(); // email -> display_name awaiting OTP
  entries = new Map<string, Entry>(); // `${userId}:${eventId}`
  idempotency = new Map<string, unknown>(); // `${userId}:${eventId}:${key}` -> response body
  challenges = new Map<string, IssuedChallenge>();
  faults = new Map<FaultKey, ArmedFault>();
  challengeCounter = 0;

  /** Everyone is treated as having entered (lets scenarios start mid-story). */
  autoEnter = false;
  /** Everyone who won has already claimed. */
  autoClaim = false;
  /** What the (fixed) draw decided for the signed-in person. */
  outcome: 'won' | 'waitlisted' = 'won';
  /** When false the stream endpoint answers 503, forcing clients onto the polling fallback. */
  sseEnabled = true;

  /** All events: the fixed demo ones plus any created through the admin API. */
  events: EventDef[] = [];
  /**
   * Admin-driven lifecycle. Once an organizer acts on an event (or a scenario pins a
   * phase) its phase comes from here instead of from the clock and its timeline.
   */
  manual = new Map<string, { phase: Phase; drawnAt?: number }>();
  configs = new Map<string, DefenceConfig>();
  /** Admin writes replayed by Idempotency-Key. */
  adminIdempotency = new Map<string, { status: number; body: unknown }>();
  /** Demo switch: make the invariants endpoint report a violation (to show the red banner). */
  breakInvariants = false;
  createdCounter = 0;

  constructor() {
    this.reset('fresh');
  }

  reset(scenarioId: string = this.scenarioId): void {
    const scenario = SCENARIOS.find((s) => s.id === scenarioId) ?? SCENARIOS[0]!;
    this.scenarioId = scenario.id;
    this.users = new Map();
    this.pending = new Map();
    this.entries = new Map();
    this.idempotency = new Map();
    this.challenges = new Map();
    this.faults = new Map();
    this.challengeCounter = 0;
    this.autoEnter = false;
    this.autoClaim = false;
    this.outcome = 'won';
    this.sseEnabled = true;
    this.events = EVENTS.map((e) => ({ ...e }));
    this.manual = new Map();
    this.configs = new Map(this.events.map((e) => [e.id, defaultDefences(e.id)]));
    this.adminIdempotency = new Map();
    this.breakInvariants = false;
    this.createdCounter = 0;
    this.clock.setSpeed(1);
    this.clock.setIso(INITIAL_CLOCK);
    scenario.apply(this);
  }

  /** Replace the set of armed faults; faults that stay armed keep their counters. */
  setFaults(keys: FaultKey[]): void {
    const next = new Map<FaultKey, ArmedFault>();
    for (const key of keys) {
      const existing = this.faults.get(key);
      if (existing) next.set(key, existing);
      else {
        const kind = key.split(':')[1] as FaultKind;
        next.set(key, { key, remaining: kind === 'network_fail' || kind === 'server_error' ? 3 : undefined });
      }
    }
    this.faults = next;
  }

  armFault(key: FaultKey): void {
    this.setFaults([...this.faults.keys(), key]);
  }

  findEvent(id: string): EventDef | undefined {
    return this.events.find((e) => e.id === id);
  }

  setManualPhase(eventId: string, phase: Phase, drawnAt?: number): void {
    const prev = this.manual.get(eventId);
    this.manual.set(eventId, { phase, drawnAt: drawnAt ?? prev?.drawnAt });
  }

  /** When holds expire: TTL after the draw (admin-run draw, or the event's timeline). */
  holdEndsFor(ev: EventDef): number {
    const drawnAt = this.manual.get(ev.id)?.drawnAt;
    return drawnAt !== undefined ? drawnAt + ev.claim_ttl_s * 1000 : timeline(ev).holdEnds;
  }

  phaseOf(ev: EventDef): Phase {
    const m = this.manual.get(ev.id);
    if (!m) return derivePhase(ev, this.clock.now());
    // An admin-run claiming phase still ends by itself when the holds run out.
    if (m.phase === 'CLAIMING' && this.clock.now() >= this.holdEndsFor(ev)) return 'CLOSED';
    return m.phase;
  }

  entryFor(userId: string, ev: EventDef): Entry | undefined {
    const stored = this.entries.get(`${userId}:${ev.id}`);
    if (stored) return stored;
    if (this.autoEnter && ev.id === PRIMARY_EVENT_ID) {
      return { entered_at: new Date(timeline(ev).opens + 60_000).toISOString() };
    }
    return undefined;
  }

  seatFor(userId: string, ev: EventDef): number {
    return 1 + (hashInt('seat', userId, ev.id) % ev.inventory);
  }

  status(user: User, ev: EventDef): MockStatus {
    const phase = this.phaseOf(ev);
    const entry = this.entryFor(user.id, ev);
    const drawn = phase === 'CLAIMING' || phase === 'CLOSED';
    const base: MockStatus = { state: 'REGISTERED', phase };
    if (!entry) return base;

    const claim = entry.claim ?? (this.autoClaim && this.outcome === 'won' && drawn ? this.claimFor(user.id, ev) : undefined);
    if (claim) {
      return { ...base, state: 'CLAIMED', seat_no: claim.seat_no, ticket_code: claim.ticket_code, public_id: this.publicId(user.id, ev) };
    }
    if (!drawn) return { ...base, state: 'ENTERED' };

    const publicId = this.publicId(user.id, ev);
    if (this.outcome === 'won') {
      if (phase === 'CLAIMING' && this.clock.now() < this.holdEndsFor(ev)) {
        return { ...base, state: 'WON', hold_expires_at: new Date(this.holdEndsFor(ev)).toISOString(), public_id: publicId };
      }
      return { ...base, state: 'EXPIRED', public_id: publicId };
    }
    if (phase === 'CLAIMING') {
      return { ...base, state: 'WAITLISTED', waitlist_position: 1 + (hashInt('wl', user.id, ev.id) % 300), public_id: publicId };
    }
    return { ...base, state: 'LOST', public_id: publicId };
  }

  claimFor(userId: string, ev: EventDef): { seat_no: number; ticket_code: string } {
    return { seat_no: this.seatFor(userId, ev), ticket_code: ticketCode(userId, ev.id) };
  }

  publicId(userId: string, ev: EventDef): string {
    return `p_${hashHex('public', userId, ev.id).slice(0, 12)}`;
  }

  adminEventView(ev: EventDef) {
    return { ...this.eventView(ev), config: { defences: this.configs.get(ev.id) ?? defaultDefences(ev.id) } };
  }

  eventView(ev: EventDef) {
    return {
      id: ev.id,
      name: ev.name,
      description: ev.description,
      phase: this.phaseOf(ev),
      mode: ev.mode,
      inventory: ev.inventory,
      window_opens_at: ev.opens_at,
      window_closes_at: ev.closes_at,
      claim_ttl_s: ev.claim_ttl_s,
      seed_commitment: hashHex('commit', ev.id),
      server_now: this.clock.iso(),
    };
  }
}

