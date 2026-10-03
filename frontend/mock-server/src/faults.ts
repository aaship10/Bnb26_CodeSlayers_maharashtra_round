import type { FastifyReply, FastifyRequest } from 'fastify';
import { POW_BITS, type FaultKind, type FaultTarget, type IssuedChallenge, type World } from './world';
import { verifyPow } from './pow';

export const RATE_LIMIT_MS = 8_000;
export const SLOW_MS = 3_000;
export const CAPTCHA_OK_TOKEN = 'mock-captcha-ok';

export function sendError(
  reply: FastifyReply,
  status: number,
  code: string,
  message: string,
  details?: Record<string, unknown>,
  headers?: Record<string, string>,
): FastifyReply {
  if (headers) for (const [k, v] of Object.entries(headers)) reply.header(k, v);
  return reply.code(status).send({ code, message, ...(details ? { details } : {}) });
}

export function issueChallenge(w: World, type: 'pow' | 'captcha', eventId: string, userId: string): IssuedChallenge {
  w.challengeCounter += 1;
  const id = `ch_${String(w.challengeCounter).padStart(4, '0')}`;
  const expires_at = new Date(w.clock.now() + 120_000).toISOString();
  const c: IssuedChallenge =
    type === 'pow'
      ? { id, type, prefix: `fd1.${eventId}.${userId.slice(0, 8)}.${w.challengeCounter}`, difficulty_bits: POW_BITS, expires_at }
      : { id, type, expires_at };
  w.challenges.set(id, c);
  return c;
}

export function challengeView(c: IssuedChallenge) {
  return c.type === 'pow'
    ? { id: c.id, type: 'pow', expires_at: c.expires_at, pow: { algo: 'sha256-lzb', prefix: c.prefix, difficulty_bits: c.difficulty_bits } }
    : { id: c.id, type: 'captcha', expires_at: c.expires_at, captcha: { provider: 'mock', site_key: 'mock-site-key' } };
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

const ORDER: FaultKind[] = ['slow', 'network_fail', 'server_error', 'rate_limited', 'rejected', 'challenge_pow', 'challenge_captcha'];

/**
 * Applies whatever failures are armed for this endpoint. Returns true when the
 * request has been answered (or killed) and the route must stop.
 */
export async function runFaults(
  w: World,
  target: FaultTarget,
  req: FastifyRequest,
  reply: FastifyReply,
  ctx: { eventId: string; userId: string },
): Promise<boolean> {
  for (const kind of ORDER) {
    const key = `${target}:${kind}` as const;
    const fault = w.faults.get(key);
    if (!fault) continue;

    switch (kind) {
      case 'slow':
        await sleep(SLOW_MS);
        break;

      case 'network_fail':
        fault.remaining = (fault.remaining ?? 1) - 1;
        if (fault.remaining <= 0) w.faults.delete(key);
        reply.hijack();
        req.raw.socket.destroy();
        return true;

      case 'server_error':
        fault.remaining = (fault.remaining ?? 1) - 1;
        if (fault.remaining <= 0) w.faults.delete(key);
        sendError(reply, 500, 'INTERNAL', 'Injected failure (mock)');
        return true;

      case 'rate_limited': {
        const now = Date.now();
        if (fault.blockedUntil === undefined) fault.blockedUntil = now + RATE_LIMIT_MS;
        if (now < fault.blockedUntil) {
          const retryAfterMs = fault.blockedUntil - now;
          sendError(
            reply,
            429,
            'RATE_LIMITED',
            'Too many requests',
            { retry_after_ms: retryAfterMs, scope: 'user' },
            { 'Retry-After': String(Math.ceil(retryAfterMs / 1000)) },
          );
          return true;
        }
        w.faults.delete(key); // block served; recover
        break;
      }

      case 'rejected':
        sendError(reply, 403, 'REJECTED', 'Request rejected (mock)');
        return true;

      case 'challenge_pow':
      case 'challenge_captcha': {
        const type = kind === 'challenge_pow' ? 'pow' : 'captcha';
        const id = req.headers['x-challenge-id'];
        const solution = req.headers['x-challenge-solution'];
        if (typeof id === 'string' && typeof solution === 'string') {
          const issued = w.challenges.get(id);
          const fresh = issued && Date.parse(issued.expires_at) > w.clock.now();
          const ok =
            issued &&
            fresh &&
            issued.type === type &&
            (type === 'pow'
              ? verifyPow(issued.prefix ?? '', solution, issued.difficulty_bits ?? 0)
              : solution === CAPTCHA_OK_TOKEN);
          if (ok) {
            w.challenges.delete(id);
            w.faults.delete(key); // solved once; carry on
            break;
          }
          const next = issueChallenge(w, type, ctx.eventId, ctx.userId);
          sendError(reply, 403, 'CHALLENGE_REQUIRED', 'Challenge failed or expired', {
            challenge: challengeView(next),
            reason: 'invalid_solution',
          });
          return true;
        }
        const c = issueChallenge(w, type, ctx.eventId, ctx.userId);
        sendError(reply, 403, 'CHALLENGE_REQUIRED', 'Challenge required', { challenge: challengeView(c) });
        return true;
      }
    }
  }
  return false;
}
