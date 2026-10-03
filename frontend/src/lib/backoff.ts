/**
 * Backoff with jitter. Every timer-driven request in the app goes through here
 * so that 50,000 clients never line up on the same second.
 */
export type Rng = () => number;

export interface BackoffOptions {
  baseMs?: number;
  maxMs?: number;
  /** Never wait less than this (e.g. the 5 s polling floor). */
  floorMs?: number;
  /** Server-provided Retry-After. Always honoured as a minimum. */
  retryAfterMs?: number;
  /** Extra random spread added on top of retryAfterMs so a 429 wave doesn't re-sync. */
  retryAfterJitterMs?: number;
}

/**
 * "Equal jitter" exponential backoff: half the window is fixed, half is random.
 * attempt is 0-based.
 */
export function backoffDelay(attempt: number, opts: BackoffOptions = {}, rng: Rng = Math.random): number {
  const { baseMs = 1000, maxMs = 30_000, floorMs = 0, retryAfterMs, retryAfterJitterMs = 1500 } = opts;
  const cap = Math.min(maxMs, baseMs * 2 ** Math.max(0, attempt));
  let delay = cap / 2 + rng() * (cap / 2);
  delay = Math.max(delay, floorMs);
  if (retryAfterMs !== undefined && retryAfterMs > 0) {
    delay = Math.max(delay, retryAfterMs + rng() * retryAfterJitterMs);
  }
  return Math.round(delay);
}

/** ms plus up to spreadMs of random delay. For one-off timer-driven refreshes. */
export function withJitter(ms: number, spreadMs: number, rng: Rng = Math.random): number {
  return Math.round(ms + rng() * spreadMs);
}
