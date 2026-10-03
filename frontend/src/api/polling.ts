import type { Query } from '@tanstack/react-query';
import { backoffDelay, type Rng } from '@/lib/backoff';
import { isApiError } from './errors';

/** Deterministic fraction in [0, 1) from an integer seed (a 32-bit mix; good spread, no state). */
export function seededFraction(seed: number): number {
  let t = (Math.floor(seed) ^ 0x9e3779b9) >>> 0;
  t = Math.imul(t ^ (t >>> 16), 0x85ebca6b);
  t = Math.imul(t ^ (t >>> 13), 0xc2b2ae35);
  t ^= t >>> 16;
  return (t >>> 0) / 4_294_967_296;
}

type QueryLike = Pick<Query, 'state'>;

/**
 * A jittered, backing-off `refetchInterval` for TanStack Query.
 *
 * Why the jitter is SEEDED and not Math.random(): TanStack re-evaluates this
 * function on every render and restarts its timer whenever the value changes.
 * A fresh random number per call meant any component re-rendering faster than
 * the interval kept resetting the timer, so the poll never fired. The jitter is
 * derived from the time of the query's last update instead: different on every
 * client (they fetched at different milliseconds), stable within one cycle.
 */
export function pollInterval(baseMs: number, spreadMs: number, opts: { maxMs?: number; rng?: Rng } = {}) {
  const { maxMs = 60_000 } = opts;
  return (query: QueryLike): number => {
    const { fetchFailureCount, error, dataUpdatedAt, errorUpdatedAt } = query.state;
    const frac = seededFraction(((dataUpdatedAt ?? 0) + (errorUpdatedAt ?? 0) + fetchFailureCount) % 2_147_483_647);
    const rng = opts.rng ?? (() => frac);
    if (fetchFailureCount > 0) {
      return backoffDelay(fetchFailureCount, { baseMs, maxMs, floorMs: baseMs, retryAfterMs: isApiError(error) ? error.retryAfterMs : undefined }, rng);
    }
    return Math.round(baseMs + rng() * spreadMs);
  };
}
