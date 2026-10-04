import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import { PRIMARY_EVENT_ID, hashHex, timeline, type EventDef, type Phase } from './events';
import { PRESETS, validateDefences, type DefenceConfig } from './defences';
import { sendError } from './faults';
import type { SseHub } from './sse';
import type { World } from './world';

export const ADMIN_TOKEN = process.env.ADMIN_TOKEN ?? 'dev-admin-token';

/** Valid lifecycle moves. The server is the authority; the UI only mirrors this to grey out buttons. */
const TRANSITIONS: Record<'schedule' | 'open' | 'close' | 'draw', { from: Phase; to: Phase }> = {
  schedule: { from: 'DRAFT', to: 'SCHEDULED' },
  open: { from: 'SCHEDULED', to: 'OPEN' },
  close: { from: 'OPEN', to: 'DRAWING' },
  draw: { from: 'DRAWING', to: 'CLAIMING' },
};

const SYNTHETIC_ENTRANTS = 50_000;
const SYNTHETIC_IDLE = 2_300; // registered but never entered

/**
 * Crowd numbers for the demo event, derived from the clock so the dashboard moves
 * as time passes. Flagged synthetic: true, and the UI badges it as mock data.
 */
function syntheticCounts(world: World, ev: EventDef) {
  const now = world.clock.now();
  const t = timeline(ev);
  const phase = world.phaseOf(ev);
  const inv = ev.inventory;
  let entered = 0;
  if (phase === 'OPEN') {
    const frac = Math.min(1, Math.max(0, (now - t.opens) / (t.closes - t.opens)));
    entered = Math.round(SYNTHETIC_ENTRANTS * Math.pow(frac, 0.85)); // spread across the window: nobody needs to rush
  } else if (phase === 'DRAWING' || phase === 'CLAIMING' || phase === 'CLOSED') {
    entered = SYNTHETIC_ENTRANTS;
  }
  const by: Record<string, number> = { REGISTERED: SYNTHETIC_IDLE + SYNTHETIC_ENTRANTS - entered, ENTERED: 0, WON: 0, WAITLISTED: 0, CLAIMED: 0, EXPIRED: 0, LOST: 0 };
  if (phase === 'OPEN' || phase === 'DRAWING') by.ENTERED = entered;
  if (phase === 'CLAIMING' || phase === 'CLOSED') {
    const drawEnd = world.manual.get(ev.id)?.drawnAt ?? t.drawEnds;
    const elapsed = Math.max(0, now - drawEnd);
    const claimed = Math.round(inv * 0.93 * (1 - Math.exp(-elapsed / 90_000)));
    if (phase === 'CLAIMING') {
      by.CLAIMED = claimed;
      by.WON = inv - claimed;
      by.WAITLISTED = SYNTHETIC_ENTRANTS - inv;
    } else {
      const final = Math.round(inv * 0.93);
      by.CLAIMED = final;
      by.EXPIRED = inv - final;
      by.LOST = SYNTHETIC_ENTRANTS - inv;
    }
  }
  return by;
}

/** Real counts from people who actually used the mock (signed-in testers). */
function realCounts(world: World, ev: EventDef) {
  const by: Record<string, number> = { REGISTERED: 0, ENTERED: 0, WON: 0, WAITLISTED: 0, CLAIMED: 0, EXPIRED: 0, LOST: 0 };
  for (const user of world.users.values()) {
    by[world.status(user, ev).state] = (by[world.status(user, ev).state] ?? 0) + 1;
  }
  return by;
}

export function registerAdmin(app: FastifyInstance, world: World, hub: SseHub): void {
  function guard(req: FastifyRequest, reply: FastifyReply): boolean {
    const token = req.headers['x-admin-token'];
    if (typeof token !== 'string' || token.length === 0) {
      sendError(reply, 401, 'UNAUTHENTICATED', 'Admin token required');
      return false;
    }
    if (token !== ADMIN_TOKEN) {
      sendError(reply, 403, 'FORBIDDEN', 'That admin token is not valid');
      return false;
    }
    return true;
  }

  function eventOr404(req: FastifyRequest, reply: FastifyReply): EventDef | null {
    const ev = world.findEvent((req.params as { id: string }).id);
    if (!ev) {
      sendError(reply, 404, 'NOT_FOUND', 'No such event');
      return null;
    }
    return ev;
  }

  /** Admin writes must carry an Idempotency-Key; a repeat replays the original answer. */
  async function idempotent(req: FastifyRequest, reply: FastifyReply, run: () => { status: number; body: unknown }) {
    const key = req.headers['idempotency-key'];
    if (typeof key !== 'string' || key.length < 8) return sendError(reply, 400, 'VALIDATION_ERROR', 'Idempotency-Key header is required');
    const cacheKey = `${req.method} ${req.url} ${key}`;
    const hit = world.adminIdempotency.get(cacheKey);
    if (hit) return reply.code(hit.status).send(hit.body);
    const out = run();
    if (out.status < 300) world.adminIdempotency.set(cacheKey, out);
    return reply.code(out.status).send(out.body);
  }

  const err = (status: number, code: string, message: string, details?: Record<string, unknown>) => ({
    status,
    body: { code, message, ...(details ? { details } : {}) },
  });

  app.get('/admin/defence/presets', async (req, reply) => {
    if (!guard(req, reply)) return;
    return PRESETS;
  });

  app.get('/admin/events', async (req, reply) => {
    if (!guard(req, reply)) return;
    return world.events.map((e) => world.adminEventView(e));
  });

  app.get('/admin/events/:id', async (req, reply) => {
    if (!guard(req, reply)) return;
    const ev = eventOr404(req, reply);
    if (!ev) return;
    return world.adminEventView(ev);
  });

  app.post('/admin/events', async (req, reply) => {
    if (!guard(req, reply)) return;
    return idempotent(req, reply, () => {
      const b = (req.body ?? {}) as Record<string, unknown>;
      const name = typeof b.name === 'string' ? b.name.trim() : '';
      if (name.length < 1 || name.length > 120) return err(400, 'VALIDATION_ERROR', 'Name must be 1 to 120 characters', { field: 'name' });
      const inventory = b.inventory;
      if (typeof inventory !== 'number' || !Number.isInteger(inventory) || inventory < 1 || inventory > 1_000_000) {
        return err(400, 'VALIDATION_ERROR', 'Inventory must be a whole number from 1 to 1,000,000', { field: 'inventory' });
      }
      const opens = typeof b.window_opens_at === 'string' ? Date.parse(b.window_opens_at) : NaN;
      const closes = typeof b.window_closes_at === 'string' ? Date.parse(b.window_closes_at) : NaN;
      if (!Number.isFinite(opens) || !Number.isFinite(closes)) return err(400, 'VALIDATION_ERROR', 'Window times must be ISO-8601', { field: 'window_opens_at' });
      if (closes <= opens) return err(400, 'VALIDATION_ERROR', 'The window must close after it opens', { field: 'window_closes_at' });
      const ttl = b.claim_ttl_s;
      if (typeof ttl !== 'number' || ttl < 30 || ttl > 86_400) return err(400, 'VALIDATION_ERROR', 'Claim time must be 30 s to 24 h', { field: 'claim_ttl_s' });
      if (b.mode !== 'LOTTERY' && b.mode !== 'FCFS') return err(400, 'VALIDATION_ERROR', 'Mode must be LOTTERY or FCFS', { field: 'mode' });
      const defences = (b.config as { defences?: unknown } | undefined)?.defences;
      const problem = validateDefences(defences);
      if (problem) return err(400, 'VALIDATION_ERROR', problem, { field: 'config.defences' });

      world.createdCounter += 1;
      const id = `evt_${hashHex('created', name, String(world.createdCounter)).slice(0, 8)}`;
      const ev: EventDef = {
        id,
        name,
        description: typeof b.description === 'string' ? b.description : '',
        inventory,
        mode: b.mode,
        opens_at: new Date(opens).toISOString(),
        closes_at: new Date(closes).toISOString(),
        claim_ttl_s: Math.round(ttl),
      };
      world.events.push(ev);
      world.configs.set(id, defences as DefenceConfig);
      world.setManualPhase(id, 'DRAFT');
      return { status: 201, body: world.adminEventView(ev) };
    });
  });

  for (const action of Object.keys(TRANSITIONS) as (keyof typeof TRANSITIONS)[]) {
    app.post(`/admin/events/:id/${action}`, async (req, reply) => {
      if (!guard(req, reply)) return;
      const ev = eventOr404(req, reply);
      if (!ev) return;
      return idempotent(req, reply, () => {
        const { from, to } = TRANSITIONS[action];
        const phase = world.phaseOf(ev);
        if (phase !== from) {
          return err(409, 'VALIDATION_ERROR', `Can't ${action} an event that is ${phase}; it must be ${from}`, { phase, required: from });
        }
        world.setManualPhase(ev.id, to, action === 'draw' ? world.clock.now() : undefined);
        hub.tick();
        return { status: 200, body: world.adminEventView(ev) };
      });
    });
  }

  app.patch('/admin/events/:id/config', async (req, reply) => {
    if (!guard(req, reply)) return;
    const ev = eventOr404(req, reply);
    if (!ev) return;
    return idempotent(req, reply, () => {
      const defences = (req.body as { defences?: unknown } | undefined)?.defences;
      const problem = validateDefences(defences);
      if (problem) return err(400, 'VALIDATION_ERROR', problem, { field: 'defences' });
      world.configs.set(ev.id, defences as DefenceConfig);
      return { status: 200, body: world.adminEventView(ev) };
    });
  });

  app.get('/admin/events/:id/stats', async (req, reply) => {
    if (!guard(req, reply)) return;
    const ev = eventOr404(req, reply);
    if (!ev) return;
    const synthetic = ev.id === PRIMARY_EVENT_ID;
    const by = synthetic ? syntheticCounts(world, ev) : realCounts(world, ev);
    const real = realCounts(world, ev);
    if (synthetic) for (const k of Object.keys(by)) by[k] = (by[k] ?? 0) + (real[k] ?? 0);
    const claimed = by.CLAIMED ?? 0;
    const held = by.WON ?? 0;
    const entrants = (by.ENTERED ?? 0) + (by.WON ?? 0) + (by.WAITLISTED ?? 0) + claimed + (by.EXPIRED ?? 0) + (by.LOST ?? 0);
    return {
      event_id: ev.id,
      phase: world.phaseOf(ev),
      by_state: by,
      entrants,
      allocations: { inventory: ev.inventory, claimed, held, available: Math.max(0, ev.inventory - claimed - held) },
      holds: { active: held, expired: by.EXPIRED ?? 0 },
      synthetic,
      server_now: world.clock.iso(),
    };
  });

  app.get('/admin/events/:id/invariants', async (req, reply) => {
    if (!guard(req, reply)) return;
    const ev = eventOr404(req, reply);
    if (!ev) return;
    const broken = world.breakInvariants;
    const v = { oversold: broken ? 1 : 0, duplicate_users: 0, duplicate_seats: broken ? 2 : 0, orphaned_holds: 0 };
    return { ...v, passed: !broken, checked_at: world.clock.iso(), server_now: world.clock.iso() };
  });

  app.post('/admin/events/:id/reset', async (req, reply) => {
    if (!guard(req, reply)) return;
    const ev = eventOr404(req, reply);
    if (!ev) return;
    return idempotent(req, reply, () => {
      for (const key of [...world.entries.keys()]) if (key.endsWith(`:${ev.id}`)) world.entries.delete(key);
      world.manual.set(ev.id, { phase: 'DRAFT' }); // also forgets when the draw ran
      hub.tick();
      return { status: 200, body: world.adminEventView(ev) };
    });
  });

  app.post('/__mock/invariants', async (req) => {
    world.breakInvariants = (req.body as { broken?: unknown } | undefined)?.broken === true;
    return { broken: world.breakInvariants };
  });
}
