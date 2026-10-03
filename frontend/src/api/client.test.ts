import { z } from 'zod';
import { ApiClient } from './client';
import { ApiError } from './errors';
import { ServerClock } from '@/lib/serverClock';
import { shouldRetryQuery, queryRetryDelay } from './retry';

function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json', ...init.headers }, ...init });
}

function setup(handler: (url: string, init: RequestInit) => Response | Promise<Response>, token: string | null = 'tok') {
  const calls: { url: string; init: RequestInit }[] = [];
  const clock = new ServerClock();
  const client = new ApiClient({
    baseUrl: '/api',
    getToken: () => token,
    getDeviceId: () => 'device-1',
    clock,
    fetchImpl: (async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return handler(url, init);
    }) as unknown as typeof fetch,
    defaultTimeoutMs: 200,
  });
  return { client, calls, clock };
}

const schema = z.object({ ok: z.boolean() });

describe('ApiClient headers', () => {
  it('always sends X-Device-Id and the bearer token', async () => {
    const { client, calls } = setup(() => json({ ok: true }));
    await client.request('/x', { schema });
    const h = new Headers(calls[0]!.init.headers);
    expect(calls[0]!.url).toBe('/api/x');
    expect(h.get('X-Device-Id')).toBe('device-1');
    expect(h.get('Authorization')).toBe('Bearer tok');
  });

  it('omits the token when auth is false', async () => {
    const { client, calls } = setup(() => json({ ok: true }));
    await client.request('/x', { schema, auth: false });
    expect(new Headers(calls[0]!.init.headers).get('Authorization')).toBeNull();
  });

  it('sends Idempotency-Key and challenge headers when given', async () => {
    const { client, calls } = setup(() => json({ ok: true }));
    await client.request('/claim', {
      schema,
      method: 'POST',
      idempotencyKey: 'key-123',
      challenge: { id: 'ch_1', solution: '4242' },
    });
    const h = new Headers(calls[0]!.init.headers);
    expect(h.get('Idempotency-Key')).toBe('key-123');
    expect(h.get('X-Challenge-Id')).toBe('ch_1');
    expect(h.get('X-Challenge-Solution')).toBe('4242');
  });

  it('JSON-encodes bodies', async () => {
    const { client, calls } = setup(() => json({ ok: true }));
    await client.request('/x', { schema, method: 'POST', body: { a: 1 } });
    expect(calls[0]!.init.body).toBe('{"a":1}');
    expect(new Headers(calls[0]!.init.headers).get('Content-Type')).toBe('application/json');
  });
});

describe('ApiClient errors', () => {
  it('turns the {code,message,details} body into an ApiError', async () => {
    const { client } = setup(() => json({ code: 'WINDOW_CLOSED', message: 'closed', details: { x: 1 } }, { status: 409 }));
    await expect(client.request('/x', { schema })).rejects.toMatchObject({ code: 'WINDOW_CLOSED', status: 409, details: { x: 1 } });
  });

  it('reads retry_after_ms from details first, then Retry-After', async () => {
    const a = setup(() =>
      json({ code: 'RATE_LIMITED', message: 'x', details: { retry_after_ms: 1234, scope: 'ip' } }, { status: 429, headers: { 'Retry-After': '9' } }),
    );
    await expect(a.client.request('/x', { schema })).rejects.toMatchObject({ retryAfterMs: 1234 });

    const b = setup(() => json({ code: 'RATE_LIMITED', message: 'x' }, { status: 429, headers: { 'Retry-After': '9' } }));
    await expect(b.client.request('/x', { schema })).rejects.toMatchObject({ retryAfterMs: 9000 });
  });

  it('maps a non-JSON gateway error by status instead of crashing', async () => {
    const { client } = setup(() => new Response('<html>Bad gateway</html>', { status: 502 }));
    await expect(client.request('/x', { schema })).rejects.toMatchObject({ code: 'INTERNAL', status: 502 });
  });

  it('reports a dropped connection as NETWORK_ERROR', async () => {
    const { client } = setup(() => {
      throw new TypeError('Failed to fetch');
    });
    await expect(client.request('/x', { schema })).rejects.toMatchObject({ code: 'NETWORK_ERROR', status: 0 });
  });

  it('reports a hung request as TIMEOUT', async () => {
    const { client } = setup(
      (_url, init) =>
        new Promise<Response>((_res, rej) => {
          init.signal?.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError')));
        }),
    );
    await expect(client.request('/x', { schema, timeoutMs: 20 })).rejects.toMatchObject({ code: 'TIMEOUT' });
  });

  it('lets caller cancellation through untouched (not an app error)', async () => {
    const ctrl = new AbortController();
    const { client } = setup(
      (_url, init) =>
        new Promise<Response>((_res, rej) => {
          init.signal?.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError')));
        }),
    );
    const p = client.request('/x', { schema, signal: ctrl.signal, timeoutMs: 5000 });
    ctrl.abort();
    await expect(p).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('fails visibly when a response does not match the schema', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const { client } = setup(() => json({ ok: 'yes' }));
    await expect(client.request('/x', { schema })).rejects.toMatchObject({ code: 'SCHEMA_MISMATCH' });
    expect(err).toHaveBeenCalled();
    err.mockRestore();
  });

  it('treats a 200 with a non-JSON body as SCHEMA_MISMATCH', async () => {
    const { client } = setup(() => new Response('hello', { status: 200 }));
    await expect(client.request('/x', { schema })).rejects.toMatchObject({ code: 'SCHEMA_MISMATCH' });
  });
});

describe('ApiClient server clock', () => {
  it('feeds server_now from any response into the shared clock', async () => {
    const { client, clock } = setup(() => json({ ok: true, server_now: new Date(Date.now() + 60_000).toISOString() }));
    expect(clock.synced).toBe(false);
    await client.request('/x', { schema });
    expect(clock.synced).toBe(true);
    expect(clock.offsetMs).toBeGreaterThan(55_000);
    expect(clock.offsetMs).toBeLessThan(65_000);
  });
});

describe('query retry policy', () => {
  it('retries transient failures only, and only a few times', () => {
    expect(shouldRetryQuery(0, new ApiError('NETWORK_ERROR', 'x', 0))).toBe(true);
    expect(shouldRetryQuery(0, new ApiError('TIMEOUT', 'x', 0))).toBe(true);
    expect(shouldRetryQuery(0, new ApiError('INTERNAL', 'x', 503))).toBe(true);
    expect(shouldRetryQuery(0, new ApiError('RATE_LIMITED', 'x', 429))).toBe(true);
    expect(shouldRetryQuery(3, new ApiError('NETWORK_ERROR', 'x', 0))).toBe(false);
  });

  it('does not retry things retrying cannot fix', () => {
    expect(shouldRetryQuery(0, new ApiError('NOT_FOUND', 'x', 404))).toBe(false);
    expect(shouldRetryQuery(0, new ApiError('UNAUTHENTICATED', 'x', 401))).toBe(false);
    expect(shouldRetryQuery(0, new ApiError('SCHEMA_MISMATCH', 'x', 200))).toBe(false);
    expect(shouldRetryQuery(0, new Error('random'))).toBe(false);
  });

  it('waits at least Retry-After before retrying a 429', () => {
    const e = new ApiError('RATE_LIMITED', 'x', 429, undefined, 6000);
    for (let i = 0; i < 50; i++) expect(queryRetryDelay(0, e)).toBeGreaterThanOrEqual(6000);
  });
});
