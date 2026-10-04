import Fastify, { type FastifyInstance, type FastifyReply, type FastifyRequest } from 'fastify';
import { PRIMARY_EVENT_ID, hashHex, timeline, uuidFrom, type EventDef } from './events';
import { registerAdmin } from './admin';
import { registerFairness, TAMPER_MODES } from './fairness';
import { registerSim } from './sim';
import { CAPTCHA_OK_TOKEN, challengeView, issueChallenge, runFaults, sendError } from './faults';
import { TIME_PRESETS, SCENARIOS } from './scenarios';
import { SseHub } from './sse';
import { FAULT_KINDS, FAULT_TARGETS, OTP, World, parseFaultKey, type FaultKey, type User } from './world';

export interface MockApp {
  app: FastifyInstance;
  world: World;
  hub: SseHub;
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const TOKEN_TTL_MS = 60 * 60_000;

function encodeToken(u: User): string {
  return `mock.${Buffer.from(JSON.stringify({ uid: u.id, email: u.email, name: u.display_name })).toString('base64url')}`;
}

function decodeToken(token: string): User | null {
  if (!token.startsWith('mock.')) return null;
  try {
    const p = JSON.parse(Buffer.from(token.slice(5), 'base64url').toString('utf8')) as { uid: string; email: string; name: string };
    if (!p.uid || !p.email) return null;
    return { id: p.uid, email: p.email, display_name: p.name ?? p.email };
  } catch {
    return null;
  }
}

export function buildApp(world: World = new World(), opts: { simSpeed?: number } = {}): MockApp {
  const app = Fastify({ logger: false });
  const hub = new SseHub(world);

  const ticker = setInterval(() => hub.tick(), 1000);
  ticker.unref();
  app.addHook('onClose', async () => {
    clearInterval(ticker);
    hub.dropAll();
  });

  /** Bearer token (AUTH_MODE=jwt style) or X-User-Id (AUTH_MODE=dev style). */
  function authenticate(req: FastifyRequest, reply: FastifyReply): User | null {
    let user: User | null = null;
    const auth = req.headers.authorization;
    if (auth?.startsWith('Bearer ')) user = decodeToken(auth.slice(7));
    else if (typeof req.headers['x-user-id'] === 'string') {
      const id = req.headers['x-user-id'];
      user = { id, email: `${id}@dev.local`, display_name: 'Dev User' };
    }
    if (!user) {
      sendError(reply, 401, 'UNAUTHENTICATED', 'Sign in required');
      return null;
    }
    world.users.set(user.id, user);
    return user;
  }

  function eventOr404(req: FastifyRequest, reply: FastifyReply): EventDef | null {
    const id = (req.params as { id: string }).id;
    const ev = world.findEvent(id);
    if (!ev) {
      sendError(reply, 404, 'NOT_FOUND', `No such event: ${id}`);
      return null;
    }
    return ev;
  }

  /* ------------------------------------------------------------------ auth */

  app.post('/auth/register', async (req, reply) => {
    const body = (req.body ?? {}) as { email?: unknown; display_name?: unknown; hp?: unknown };
    const email = typeof body.email === 'string' ? body.email.trim().toLowerCase() : '';
    const name = typeof body.display_name === 'string' ? body.display_name.trim() : '';
    if (!EMAIL_RE.test(email)) return sendError(reply, 400, 'VALIDATION_ERROR', 'Enter a valid email address', { field: 'email' });
    if (name.length < 1 || name.length > 60) {
      return sendError(reply, 400, 'VALIDATION_ERROR', 'Enter a name between 1 and 60 characters', { field: 'display_name' });
    }
    // Honeypot: real people leave it empty. Bots get the same 202 and nothing happens.
    if (body.hp !== '' && body.hp !== undefined) return reply.code(202).send({});
    world.pending.set(email, name);
    console.log(`[mock] OTP for ${email}: ${OTP}`);
    return reply.code(202).send({});
  });

  app.post('/auth/verify', async (req, reply) => {
    const body = (req.body ?? {}) as { email?: unknown; otp?: unknown };
    const email = typeof body.email === 'string' ? body.email.trim().toLowerCase() : '';
    const name = world.pending.get(email);
    if (name === undefined) {
      return sendError(reply, 400, 'VALIDATION_ERROR', 'No sign-up is waiting for that email. Start again.', { field: 'email' });
    }
    if (body.otp !== OTP) return sendError(reply, 400, 'VALIDATION_ERROR', 'That code is not right', { field: 'otp' });
    world.pending.delete(email);
    const user: User = { id: uuidFrom(email), email, display_name: name };
    world.users.set(user.id, user);
    return {
      token: encodeToken(user),
      expires_at: new Date(Date.now() + TOKEN_TTL_MS).toISOString(),
      user_id: user.id,
    };
  });

  app.post('/auth/refresh', async (req, reply) => {
    const user = authenticate(req, reply);
    if (!user) return;
    return { token: encodeToken(user), expires_at: new Date(Date.now() + TOKEN_TTL_MS).toISOString(), user_id: user.id };
  });

  app.get('/auth/me', async (req, reply) => {
    const user = authenticate(req, reply);
    if (!user) return;
    return { user_id: user.id, email: user.email, display_name: user.display_name };
  });

  app.post('/defence/challenge', async (req, reply) => {
    const user = authenticate(req, reply);
    if (!user) return;
    const eventId = (req.body as { event_id?: unknown } | undefined)?.event_id;
    if (typeof eventId !== 'string' || !world.findEvent(eventId)) {
      return sendError(reply, 400, 'VALIDATION_ERROR', 'event_id is required', { field: 'event_id' });
    }
    const wantsCaptcha = [...world.faults.keys()].some((k) => k.endsWith(':challenge_captcha'));
    return challengeView(issueChallenge(world, wantsCaptcha ? 'captcha' : 'pow', eventId, user.id));
  });

  /* ---------------------------------------------------------------- events */

  // DRAFT events are the organizer's business; attendees don't see them.
  app.get('/events', async () => world.events.filter((e) => world.phaseOf(e) !== 'DRAFT').map((e) => world.eventView(e)));

  app.get('/events/:id', async (req, reply) => {
    const ev = eventOr404(req, reply);
    if (!ev) return;
    return world.eventView(ev);
  });

  app.post('/events/:id/enter', async (req, reply) => {
    const user = authenticate(req, reply);
    if (!user) return;
    const ev = eventOr404(req, reply);
    if (!ev) return;
    if (await runFaults(world, 'enter', req, reply, { eventId: ev.id, userId: user.id })) return;

    const phase = world.phaseOf(ev);
    if (phase === 'DRAFT' || phase === 'SCHEDULED') return sendError(reply, 409, 'WINDOW_NOT_OPEN', 'The entry window has not opened');
    if (phase !== 'OPEN') return sendError(reply, 409, 'WINDOW_CLOSED', 'The entry window has closed');

    const existing = world.entryFor(user.id, ev);
    if (existing) return { state: 'ENTERED', entered_at: existing.entered_at, already_entered: true };
    const entered_at = world.clock.iso();
    world.entries.set(`${user.id}:${ev.id}`, { entered_at });
    return { state: 'ENTERED', entered_at, already_entered: false };
  });

  app.get('/events/:id/status', async (req, reply) => {
    const user = authenticate(req, reply);
    if (!user) return;
    const ev = eventOr404(req, reply);
    if (!ev) return;
    if (await runFaults(world, 'status', req, reply, { eventId: ev.id, userId: user.id })) return;
    return { ...world.status(user, ev), server_now: world.clock.iso() };
  });

  app.post('/events/:id/claim', async (req, reply) => {
    const user = authenticate(req, reply);
    if (!user) return;
    const ev = eventOr404(req, reply);
    if (!ev) return;
    const key = req.headers['idempotency-key'];
    if (typeof key !== 'string' || key.length < 8) {
      return sendError(reply, 400, 'VALIDATION_ERROR', 'Idempotency-Key header is required', { field: 'Idempotency-Key' });
    }
    if (await runFaults(world, 'claim', req, reply, { eventId: ev.id, userId: user.id })) return;

    // A retry with the same key gets the original answer, never a second seat.
    const cacheKey = `${user.id}:${ev.id}:${key}`;
    const replay = world.idempotency.get(cacheKey);
    if (replay) return replay;

    const status = world.status(user, ev);
    switch (status.state) {
      case 'WON': {
        const claim = world.claimFor(user.id, ev);
        const entry = world.entryFor(user.id, ev)!;
        world.entries.set(`${user.id}:${ev.id}`, { ...entry, claim });
        const body = { state: 'CLAIMED', seat_no: claim.seat_no, ticket_code: claim.ticket_code };
        world.idempotency.set(cacheKey, body);
        return body;
      }
      case 'CLAIMED':
        return sendError(reply, 409, 'ALREADY_CLAIMED', 'You already claimed a seat');
      case 'EXPIRED':
        return sendError(reply, 410, 'HOLD_EXPIRED', 'Your hold has expired');
      default:
        return sendError(reply, 403, 'NOT_WINNER', 'No seat is being held for you');
    }
  });

  app.get('/events/:id/stream', async (req, reply) => {
    const user = authenticate(req, reply);
    if (!user) return;
    const ev = eventOr404(req, reply);
    if (!ev) return;
    if (await runFaults(world, 'status', req, reply, { eventId: ev.id, userId: user.id })) return;
    if (!world.sseEnabled) return sendError(reply, 503, 'INTERNAL', 'Live updates are switched off (mock)');

    const raw = req.headers['last-event-id'];
    const parsed = typeof raw === 'string' ? Number.parseInt(raw, 10) : NaN;
    reply.hijack();
    hub.open(reply.raw, user, ev, Number.isFinite(parsed) ? parsed : null);
  });

  /* ------------------------------------------------- mock control (dev only) */

  const controlState = () => {
    const t = timeline(world.findEvent(PRIMARY_EVENT_ID)!);
    return {
      server_now: world.clock.iso(),
      speed: world.clock.speed,
      scenario: world.scenarioId,
      scenarios: SCENARIOS.map(({ id, name, description, group }) => ({ id, name, description, group })),
      time_presets: TIME_PRESETS.map((p) => ({ id: p.id, label: p.label, iso: new Date(p.at).toISOString() })),
      faults: [...world.faults.keys()],
      fault_targets: FAULT_TARGETS,
      fault_kinds: FAULT_KINDS,
      sse_clients: hub.clientCount,
      sse_enabled: world.sseEnabled,
      invariants_broken: world.breakInvariants,
      tamper: world.tamper,
      tamper_modes: TAMPER_MODES,
      admin_token: 'dev-admin-token (or $ADMIN_TOKEN)',
      otp: OTP,
      captcha_token: CAPTCHA_OK_TOKEN,
      timeline: {
        opens: new Date(t.opens).toISOString(),
        closes: new Date(t.closes).toISOString(),
        hold_ends: new Date(t.holdEnds).toISOString(),
      },
      fingerprint: hashHex('state', world.scenarioId).slice(0, 8),
    };
  };

  app.get('/__mock/state', async () => controlState());

  app.post('/__mock/scenario', async (req, reply) => {
    const id = (req.body as { id?: unknown } | undefined)?.id;
    if (typeof id !== 'string' || !SCENARIOS.some((s) => s.id === id)) {
      return sendError(reply, 400, 'VALIDATION_ERROR', `Unknown scenario: ${String(id)}`);
    }
    world.reset(id);
    hub.tick();
    return controlState();
  });

  app.post('/__mock/reset', async () => {
    world.reset();
    hub.tick();
    return controlState();
  });

  app.post('/__mock/clock', async (req, reply) => {
    const b = (req.body ?? {}) as { set?: unknown; advance_ms?: unknown; speed?: unknown };
    if (typeof b.speed === 'number' && b.speed >= 0 && b.speed <= 600) world.clock.setSpeed(b.speed);
    if (typeof b.set === 'string') {
      if (!Number.isFinite(Date.parse(b.set))) return sendError(reply, 400, 'VALIDATION_ERROR', 'set must be an ISO time');
      world.clock.setIso(b.set);
    }
    if (typeof b.advance_ms === 'number') world.clock.advance(b.advance_ms);
    hub.tick();
    return controlState();
  });

  app.post('/__mock/faults', async (req, reply) => {
    const list = (req.body as { faults?: unknown } | undefined)?.faults;
    if (!Array.isArray(list)) return sendError(reply, 400, 'VALIDATION_ERROR', 'faults must be an array');
    const keys: FaultKey[] = [];
    for (const k of list) {
      const key = typeof k === 'string' ? parseFaultKey(k) : null;
      if (!key) return sendError(reply, 400, 'VALIDATION_ERROR', `Unknown fault: ${String(k)}`);
      keys.push(key);
    }
    world.setFaults(keys);
    return controlState();
  });

  app.post('/__mock/sse/drop', async () => ({ dropped: hub.dropAll() }));

  app.post('/__mock/sse', async (req, reply) => {
    const enabled = (req.body as { enabled?: unknown } | undefined)?.enabled;
    if (typeof enabled !== 'boolean') return sendError(reply, 400, 'VALIDATION_ERROR', 'enabled must be a boolean');
    world.sseEnabled = enabled;
    if (!enabled) hub.dropAll();
    return controlState();
  });

  registerAdmin(app, world, hub);
  registerFairness(app, world);
  registerSim(app, { speed: opts.simSpeed });

  return { app, world, hub };
}
