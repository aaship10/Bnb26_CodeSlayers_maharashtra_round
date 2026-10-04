import { createHash } from 'node:crypto';
import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import { PRIMARY_EVENT_ID, timeline, type EventDef } from './events';
import { sendError } from './faults';
import { chain, demoEntrants, mockBeacon, mockServerSeed, runDraw, type AuditRecord, type DrawResult, type Entrant } from './draw';
import type { World } from './world';

export const ALGORITHM_VERSION = 'fd-draw/1-provisional';
export const TAMPER_MODES = ['none', 'entrants', 'results', 'seed', 'audit'] as const;
export type TamperMode = (typeof TAMPER_MODES)[number];

/**
 * Public fairness data: commitment before the window, entrant list after it
 * closes, seed + beacon + results after the draw, and an audit hash chain.
 * The demo event uses 50,000 synthetic entrants (flagged synthetic: true);
 * other events use whoever actually entered through the mock.
 *
 * Tamper modes (dev panel) make the served data dishonest so the browser
 * verifier can be seen catching it.
 */
export function registerFairness(app: FastifyInstance, world: World): void {
  const drawCache = new Map<string, DrawResult>();

  const entrantsOf = (ev: EventDef): Entrant[] => {
    if (ev.id === PRIMARY_EVENT_ID) return demoEntrants();
    const out: Entrant[] = [];
    for (const [key] of world.entries) {
      if (!key.endsWith(`:${ev.id}`)) continue;
      out.push({ public_id: world.publicId(key.slice(0, key.length - ev.id.length - 1), ev), weight: 1 });
    }
    return out;
  };

  const drawFor = (ev: EventDef): DrawResult => {
    const entrants = entrantsOf(ev);
    const cacheKey = `${ev.id}:${entrants.length}:${ev.inventory}`;
    let d = drawCache.get(cacheKey);
    if (!d) {
      d = runDraw(ev.id, mockServerSeed(ev.id), mockBeacon(ev.id).randomness, entrants, ev.inventory);
      drawCache.set(cacheKey, d);
    }
    return d;
  };

  const stage = (ev: EventDef) => {
    const phase = world.phaseOf(ev);
    return {
      phase,
      closed: phase === 'DRAWING' || phase === 'CLAIMING' || phase === 'CLOSED',
      drawn: phase === 'CLAIMING' || phase === 'CLOSED',
    };
  };

  /** The honest chain for an event, as far as its lifecycle has got. */
  const honestChain = (ev: EventDef): AuditRecord[] => {
    const s = stage(ev);
    const t = timeline(ev);
    const beacon = mockBeacon(ev.id);
    const iso = (ms: number) => new Date(ms).toISOString();
    const recs: { type: string; ts: string; payload: Record<string, unknown> }[] = [
      { type: 'EVENT_CREATED', ts: iso(t.opens - 86_400_000), payload: { name: ev.name, inventory: ev.inventory, mode: ev.mode } },
      { type: 'CONFIG_SET', ts: iso(t.opens - 86_400_000 + 60_000), payload: { defences: world.configs.get(ev.id)?.preset ?? 'unknown' } },
      { type: 'SEED_COMMITTED', ts: iso(t.opens - 3_600_000), payload: { commitment: runDrawCommitment(ev) } },
      { type: 'BEACON_ROUND_ANNOUNCED', ts: iso(t.opens - 3_600_000 + 1000), payload: { source: 'mock-beacon', round: beacon.round } },
    ];
    if (s.phase !== 'DRAFT' && s.phase !== 'SCHEDULED') recs.push({ type: 'WINDOW_OPENED', ts: iso(t.opens), payload: {} });
    if (s.closed) {
      const d = drawFor(ev);
      recs.push({ type: 'WINDOW_CLOSED', ts: iso(t.closes), payload: { entrants_count: d.order.length, entrants_hash: d.entrants_hash } });
    }
    if (s.drawn) {
      const d = drawFor(ev);
      const at = world.manual.get(ev.id)?.drawnAt ?? t.drawEnds;
      recs.push({ type: 'BEACON_FETCHED', ts: iso(at - 4000), payload: { round: beacon.round, randomness: beacon.randomness.toString('hex') } });
      recs.push({ type: 'SEED_REVEALED', ts: iso(at - 2000), payload: { server_seed: mockServerSeed(ev.id).toString('hex') } });
      recs.push({
        type: 'DRAW_COMPLETED',
        ts: iso(at),
        payload: {
          algorithm_version: ALGORITHM_VERSION,
          final_seed: d.final_seed,
          winners_count: d.winners.length,
          waitlist_count: d.waitlist.length,
          winners_hash: d.winners_hash,
          waitlist_hash: d.waitlist_hash,
        },
      });
    }
    return chain(recs);
  };

  const runDrawCommitment = (ev: EventDef) => drawCommitment(ev.id);

  /** What we serve: the honest chain, or one with an edited record in "audit" tamper mode. */
  const servedChain = (ev: EventDef): AuditRecord[] => {
    const honest = honestChain(ev);
    if (world.tamper !== 'audit' || honest.length < 3) return honest;
    return honest.map((r) => (r.seq === 2 ? { ...r, payload: { ...r.payload, defences: 'none' } } : r));
  };

  const eventOr404 = (req: FastifyRequest, reply: FastifyReply): EventDef | null => {
    const ev = world.findEvent((req.params as { id: string }).id);
    if (!ev) {
      sendError(reply, 404, 'NOT_FOUND', 'No such event');
      return null;
    }
    return ev;
  };

  app.get('/events/:id/fairness', async (req, reply) => {
    const ev = eventOr404(req, reply);
    if (!ev) return;
    const s = stage(ev);
    const beacon = mockBeacon(ev.id);
    const d = s.closed ? drawFor(ev) : null;
    const chainNow = honestChain(ev);
    return {
      event_id: ev.id,
      phase: s.phase,
      algorithm_version: ALGORITHM_VERSION,
      seed_commitment: drawCommitment(ev.id),
      server_seed: s.drawn ? (world.tamper === 'seed' ? 'f'.repeat(64) : mockServerSeed(ev.id).toString('hex')) : null,
      beacon: { source: 'mock-beacon', round: beacon.round, randomness: s.drawn ? beacon.randomness.toString('hex') : null },
      entrants_hash: d ? d.entrants_hash : null,
      entrants_count: d ? d.order.length : null,
      final_seed: s.drawn && d ? d.final_seed : null,
      inventory: ev.inventory,
      result:
        s.drawn && d
          ? { winners_count: d.winners.length, waitlist_count: d.waitlist.length, winners_hash: d.winners_hash, waitlist_hash: d.waitlist_hash }
          : null,
      audit_head_hash: chainNow[chainNow.length - 1]?.hash ?? null,
      synthetic: ev.id === PRIMARY_EVENT_ID,
      server_now: world.clock.iso(),
    };
  });

  app.get('/events/:id/fairness/entrants', async (req, reply) => {
    const ev = eventOr404(req, reply);
    if (!ev) return;
    if (!stage(ev).closed) return sendError(reply, 409, 'WINDOW_NOT_OPEN', 'The entrant list is published when the window closes');
    let entrants = [...entrantsOf(ev)].sort((a, b) => (a.public_id < b.public_id ? -1 : a.public_id > b.public_id ? 1 : 0));
    if (world.tamper === 'entrants' && entrants.length > 10) {
      // Swap one entrant for someone who never entered (keeps the list sorted and unique).
      const victim = entrants[7]!;
      entrants = entrants.map((e) => (e === victim ? { ...e, public_id: `${victim.public_id.slice(0, -1)}${victim.public_id.endsWith('z') ? 'y' : 'z'}` } : e));
      entrants.sort((a, b) => (a.public_id < b.public_id ? -1 : a.public_id > b.public_id ? 1 : 0));
    }
    return { event_id: ev.id, count: entrants.length, entrants };
  });

  app.get('/events/:id/fairness/results', async (req, reply) => {
    const ev = eventOr404(req, reply);
    if (!ev) return;
    if (!stage(ev).drawn) return sendError(reply, 409, 'WINDOW_NOT_OPEN', 'Results are published after the draw');
    const d = drawFor(ev);
    let winners = d.winners;
    if (world.tamper === 'results' && winners.length > 2 && d.waitlist.length > 0) {
      // Quietly promote the first waitlisted person over winner #2.
      winners = [winners[0]!, d.waitlist[0]!, ...winners.slice(2)];
    }
    return { event_id: ev.id, winners, waitlist: d.waitlist };
  });

  app.get('/events/:id/audit', async (req, reply) => {
    const ev = eventOr404(req, reply);
    if (!ev) return;
    const q = req.query as { from_seq?: string; limit?: string };
    const from = Math.max(1, Number.parseInt(q.from_seq ?? '1', 10) || 1);
    const limit = Math.min(200, Math.max(1, Number.parseInt(q.limit ?? '50', 10) || 50));
    const all = servedChain(ev);
    const records = all.filter((r) => r.seq >= from).slice(0, limit);
    const last = records[records.length - 1];
    const head = all[all.length - 1];
    return {
      event_id: ev.id,
      records,
      next_from_seq: last && last.seq < all.length ? last.seq + 1 : null,
      head: head ? { seq: head.seq, hash: head.hash } : null,
    };
  });

  // In "audit" tamper mode this deliberately reports on the HONEST chain: a server that
  // claims everything is fine while serving edited records. The browser check catches it.
  app.get('/events/:id/audit/verify', async (req, reply) => {
    const ev = eventOr404(req, reply);
    if (!ev) return;
    const all = honestChain(ev);
    const head = all[all.length - 1];
    return { ok: true, head_seq: head?.seq ?? 0, head_hash: head?.hash ?? '0'.repeat(64), checked: all.length, first_bad_seq: null };
  });

  app.post('/__mock/tamper', async (req, reply) => {
    const mode = (req.body as { mode?: unknown } | undefined)?.mode;
    if (typeof mode !== 'string' || !(TAMPER_MODES as readonly string[]).includes(mode)) {
      return sendError(reply, 400, 'VALIDATION_ERROR', `mode must be one of ${TAMPER_MODES.join(', ')}`);
    }
    world.tamper = mode as TamperMode;
    return { tamper: world.tamper };
  });
}

/** SHA-256 of the event's server seed: what is published before the window opens. */
export function drawCommitment(eventId: string): string {
  return createHash('sha256').update(mockServerSeed(eventId)).digest('hex');
}
