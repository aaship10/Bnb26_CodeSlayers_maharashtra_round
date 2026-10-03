import type { z } from 'zod';
import { ApiError } from './errors';
import { errorBodySchema } from './schemas';
import { ServerClock } from '@/lib/serverClock';

export interface ApiClientConfig {
  baseUrl: string;
  getToken: () => string | null;
  getDeviceId: () => string;
  clock: ServerClock;
  fetchImpl?: typeof fetch;
  defaultTimeoutMs?: number;
}

export interface ChallengeSolution {
  id: string;
  /** Decimal nonce for pow, provider token for captcha. */
  solution: string;
}

export interface RequestOptions<T> {
  schema: z.ZodType<T>;
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  body?: unknown;
  headers?: Record<string, string>;
  /** Claim and admin writes. Reuse the same value when retrying. */
  idempotencyKey?: string;
  /** Sent as X-Challenge-Id / X-Challenge-Solution when retrying after CHALLENGE_REQUIRED. */
  challenge?: ChallengeSolution;
  /** Send the bearer token. Default true. */
  auth?: boolean;
  signal?: AbortSignal;
  timeoutMs?: number;
}

const FALLBACK_CODES: Record<number, string> = {
  400: 'VALIDATION_ERROR',
  401: 'UNAUTHENTICATED',
  403: 'FORBIDDEN',
  404: 'NOT_FOUND',
  429: 'RATE_LIMITED',
};

function parseRetryAfter(header: string | null, details?: Record<string, unknown>): number | undefined {
  const fromBody = details?.retry_after_ms;
  if (typeof fromBody === 'number' && Number.isFinite(fromBody) && fromBody >= 0) return fromBody;
  if (header) {
    const secs = Number(header);
    if (Number.isFinite(secs) && secs >= 0) return Math.round(secs * 1000);
    const date = Date.parse(header);
    if (Number.isFinite(date)) return Math.max(0, date - Date.now());
  }
  return undefined;
}

export class ApiClient {
  private readonly fetchImpl: typeof fetch;

  constructor(private readonly cfg: ApiClientConfig) {
    this.fetchImpl = cfg.fetchImpl ?? ((...args) => fetch(...args));
  }

  get clock(): ServerClock {
    return this.cfg.clock;
  }

  buildHeaders(opts: Pick<RequestOptions<unknown>, 'auth' | 'headers' | 'idempotencyKey' | 'challenge' | 'body'>): Headers {
    const h = new Headers({ Accept: 'application/json' });
    h.set('X-Device-Id', this.cfg.getDeviceId());
    if (opts.body !== undefined) h.set('Content-Type', 'application/json');
    if (opts.auth !== false) {
      const token = this.cfg.getToken();
      if (token) h.set('Authorization', `Bearer ${token}`);
    }
    if (opts.idempotencyKey) h.set('Idempotency-Key', opts.idempotencyKey);
    if (opts.challenge) {
      h.set('X-Challenge-Id', opts.challenge.id);
      h.set('X-Challenge-Solution', opts.challenge.solution);
    }
    for (const [k, v] of Object.entries(opts.headers ?? {})) h.set(k, v);
    return h;
  }

  async request<T>(path: string, opts: RequestOptions<T>): Promise<T> {
    const method = opts.method ?? 'GET';
    const url = `${this.cfg.baseUrl}${path}`;

    // Our own timeout, plus the caller's signal (React Query cancellation, route changes).
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, opts.timeoutMs ?? this.cfg.defaultTimeoutMs ?? 15_000);
    const onAbort = () => controller.abort();
    if (opts.signal) {
      if (opts.signal.aborted) controller.abort();
      else opts.signal.addEventListener('abort', onAbort, { once: true });
    }

    const sentAt = Date.now();
    let res: Response;
    try {
      res = await this.fetchImpl(url, {
        method,
        headers: this.buildHeaders(opts),
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        signal: controller.signal,
        credentials: 'same-origin',
      });
    } catch (cause) {
      if (timedOut) throw new ApiError('TIMEOUT', 'The request timed out', 0, undefined, undefined, cause);
      if (opts.signal?.aborted) throw cause; // caller cancelled; not an error to show
      throw new ApiError('NETWORK_ERROR', 'Network request failed', 0, undefined, undefined, cause);
    } finally {
      clearTimeout(timer);
      opts.signal?.removeEventListener('abort', onAbort);
    }
    const receivedAt = Date.now();

    const text = await res.text().catch(() => '');
    let json: unknown;
    let jsonOk = true;
    if (text) {
      try {
        json = JSON.parse(text);
      } catch {
        jsonOk = false;
      }
    }

    if (!res.ok) {
      const parsed = errorBodySchema.safeParse(json);
      if (parsed.success) {
        const { code, message, details } = parsed.data;
        throw new ApiError(code, message, res.status, details, parseRetryAfter(res.headers.get('Retry-After'), details));
      }
      // Not our error shape (e.g. a proxy's HTML 502): map by status, still visibly.
      const code = FALLBACK_CODES[res.status] ?? (res.status >= 500 ? 'INTERNAL' : 'UNKNOWN');
      throw new ApiError(code, `HTTP ${res.status}`, res.status, undefined, parseRetryAfter(res.headers.get('Retry-After')));
    }

    if (!jsonOk) {
      throw new ApiError('SCHEMA_MISMATCH', 'Response was not valid JSON', res.status);
    }

    if (json && typeof json === 'object' && 'server_now' in json && typeof (json as { server_now: unknown }).server_now === 'string') {
      this.cfg.clock.observe((json as { server_now: string }).server_now, sentAt, receivedAt);
    }

    const result = opts.schema.safeParse(json);
    if (!result.success) {
      // Fail visibly: the console gets the full zod report, the UI gets SCHEMA_MISMATCH.
      console.error(`[api] ${method} ${path} response failed validation`, result.error.issues, json);
      throw new ApiError('SCHEMA_MISMATCH', result.error.issues[0]?.message ?? 'Invalid response', res.status, {
        issues: result.error.issues,
      });
    }
    return result.data;
  }
}
