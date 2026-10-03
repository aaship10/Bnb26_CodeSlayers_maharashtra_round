import { backoffDelay } from '@/lib/backoff';
import { isApiError } from './errors';

export interface RetryOptions {
  /** Extra tries after the first. */
  retries?: number;
  signal?: AbortSignal;
  onRetry?: (attempt: number, error: unknown) => void;
  /** Injected in tests. */
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
}

/** A dropped connection, a timeout or a 5xx: things another try can fix. Not 4xx, not 429 (that has its own countdown). */
export function isTransient(error: unknown): boolean {
  if (!isApiError(error)) return false;
  if (error.code === 'NETWORK_ERROR' || error.code === 'TIMEOUT') return true;
  return error.status >= 500;
}

export function abortableSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException('aborted', 'AbortError'));
    const t = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(t);
      reject(new DOMException('aborted', 'AbortError'));
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

/**
 * Automatic retry for IDEMPOTENT calls only (status, enter, and claim with its
 * reused Idempotency-Key). Backoff has jitter so a wave of failures can't
 * resynchronise into a herd.
 */
export async function retryTransient<T>(fn: () => Promise<T>, opts: RetryOptions = {}): Promise<T> {
  const { retries = 2, signal, onRetry, sleep = abortableSleep } = opts;
  for (let attempt = 0; ; attempt++) {
    try {
      return await fn();
    } catch (e) {
      if (attempt >= retries || !isTransient(e)) throw e;
      onRetry?.(attempt + 1, e);
      await sleep(backoffDelay(attempt, { baseMs: 1200, maxMs: 8000, floorMs: 800 }), signal);
    }
  }
}
