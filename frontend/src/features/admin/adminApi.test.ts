// @vitest-environment jsdom
import { adminApi } from './adminApi';
import { adminToken } from './adminToken';
import { sessionStore as session } from '@/state/session';

const presets = [
  {
    id: 'none',
    name: 'None',
    description: 'x',
    defences: {
      preset: 'none',
      layers: { rate_limit: { enabled: false }, pow: { enabled: false }, captcha: { enabled: false }, signals: { enabled: false }, risk: { enabled: false } },
    },
  },
];

function stubFetch(status: number, body: unknown) {
  const calls: { url: string; headers: Headers }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, headers: new Headers(init.headers) });
      return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
    }),
  );
  return calls;
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});
afterEach(() => {
  vi.unstubAllGlobals();
  session.clear();
  adminToken.clear();
});

describe('admin token', () => {
  it('lives in sessionStorage only, never localStorage', () => {
    adminToken.set('secret-123');
    expect(window.sessionStorage.getItem('fd.admin_token')).toBe('secret-123');
    expect(JSON.stringify({ ...window.localStorage })).not.toContain('secret-123');
    adminToken.clear();
    expect(window.sessionStorage.getItem('fd.admin_token')).toBeNull();
  });

  it('is sent as X-Admin-Token; the attendee bearer token is NOT sent on admin calls', async () => {
    session.set({ token: 'attendee-token' });
    adminToken.set('secret-123');
    const calls = stubFetch(200, presets);
    await adminApi.presets();
    expect(calls[0]!.url).toBe('/api/admin/defence/presets');
    expect(calls[0]!.headers.get('X-Admin-Token')).toBe('secret-123');
    expect(calls[0]!.headers.get('Authorization')).toBeNull();
    expect(calls[0]!.url).not.toContain('secret');
  });

  it('a rejected token is forgotten, with a reason for the unlock dialog', async () => {
    adminToken.set('stale');
    stubFetch(403, { code: 'FORBIDDEN', message: 'nope' });
    await expect(adminApi.events()).rejects.toMatchObject({ code: 'FORBIDDEN' });
    expect(adminToken.get()).toBeNull();
    expect(adminToken.reason).toMatch(/not accepted/);
  });

  it('verifyToken checks a candidate without storing it, and does not wipe the current one on failure', async () => {
    adminToken.set('current');
    const calls = stubFetch(401, { code: 'UNAUTHENTICATED', message: 'nope' });
    await expect(adminApi.verifyToken('candidate')).rejects.toMatchObject({ code: 'UNAUTHENTICATED' });
    expect(calls[0]!.headers.get('X-Admin-Token')).toBe('candidate');
    expect(adminToken.get()).toBe('current');
  });

  it('admin writes carry an Idempotency-Key', async () => {
    adminToken.set('t');
    const calls = stubFetch(400, { code: 'VALIDATION_ERROR', message: 'x' });
    await expect(adminApi.transition('e1', 'open', 'key-abc-123')).rejects.toBeTruthy();
    expect(calls[0]!.url).toBe('/api/admin/events/e1/open');
    expect(calls[0]!.headers.get('Idempotency-Key')).toBe('key-abc-123');
  });
});
