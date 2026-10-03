// @vitest-environment jsdom
import { clearClaimKey, createSafeStore, getClaimKey, getDeviceId, loadSession, saveSession, clearSession } from './storage';
import { uuidv4 } from './ids';

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe('device id', () => {
  it('is created once and then stable across reads', () => {
    const a = getDeviceId();
    expect(a).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(getDeviceId()).toBe(a);
    expect(window.localStorage.getItem('fd.device_id')).toBe(a);
  });
});

describe('claim idempotency key', () => {
  it('is reused across refreshes (same sessionStorage) until cleared', () => {
    const first = getClaimKey('evt1');
    expect(getClaimKey('evt1')).toBe(first);
    expect(window.sessionStorage.getItem('fd.claim_key.evt1')).toBe(first);
  });

  it('is per event', () => {
    expect(getClaimKey('evt1')).not.toBe(getClaimKey('evt2'));
  });

  it('is replaced with a fresh key after success clears it', () => {
    const first = getClaimKey('evt1');
    clearClaimKey('evt1');
    expect(getClaimKey('evt1')).not.toBe(first);
  });

  it('never lands in localStorage (it must not outlive the tab session)', () => {
    getClaimKey('evt1');
    expect(window.localStorage.getItem('fd.claim_key.evt1')).toBeNull();
  });
});

describe('session token', () => {
  it('round-trips and clears', () => {
    expect(loadSession()).toBeNull();
    saveSession({ token: 't', user_id: 'u' });
    expect(loadSession()).toEqual({ token: 't', user_id: 'u' });
    clearSession();
    expect(loadSession()).toBeNull();
  });

  it('ignores corrupted stored data', () => {
    window.localStorage.setItem('fd.token', '{nope');
    expect(loadSession()).toBeNull();
  });
});

describe('safe store', () => {
  it('keeps working in memory when storage throws (private windows, blocked site data)', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError');
    });
    const store = createSafeStore('local');
    expect(() => store.set('k', 'v')).not.toThrow();
    expect(store.get('k')).toBe('v');
    store.remove('k');
    expect(store.get('k')).toBeNull();
    spy.mockRestore();
  });
});

describe('uuidv4', () => {
  it('works without crypto.randomUUID (insecure origins such as http://<lan-ip>)', () => {
    const original = crypto.randomUUID;
    // @ts-expect-error simulate an insecure context
    crypto.randomUUID = undefined;
    const id = uuidv4();
    crypto.randomUUID = original;
    expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  });

  it('does not repeat', () => {
    expect(new Set(Array.from({ length: 500 }, uuidv4)).size).toBe(500);
  });
});
