import { backoffDelay } from '@/lib/backoff';
import { isApiError } from './errors';

const MAX_AUTO_RETRIES = 3;

/**
 * Which failures an *idempotent read* may retry automatically.
 * Network blips, timeouts, 5xx and 429 yes; anything the server said about
 * our request (4xx) or a contract mismatch no: retrying can't fix those.
 * Mutations are never retried through this path (claim retries reuse their
 * Idempotency-Key explicitly, in the claim flow).
 */
export function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  if (failureCount >= MAX_AUTO_RETRIES) return false;
  if (!isApiError(error)) return false;
  if (error.code === 'NETWORK_ERROR' || error.code === 'TIMEOUT') return true;
  if (error.code === 'RATE_LIMITED') return true;
  return error.status >= 500;
}

export function queryRetryDelay(failureCount: number, error: unknown): number {
  const retryAfterMs = isApiError(error) ? error.retryAfterMs : undefined;
  return backoffDelay(failureCount, { baseMs: 1000, maxMs: 20_000, retryAfterMs });
}
