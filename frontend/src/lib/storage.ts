import { uuidv4 } from './ids';

/**
 * Storage that never throws. localStorage/sessionStorage can be missing or
 * throw (private windows, blocked site data), so each store keeps a memory copy
 * and the app keeps working for that tab.
 */
export interface SafeStore {
  get(key: string): string | null;
  set(key: string, value: string): void;
  remove(key: string): void;
}

export function createSafeStore(kind: 'local' | 'session'): SafeStore {
  const memory = new Map<string, string>();
  const backing = (): Storage | null => {
    try {
      const s = kind === 'local' ? window.localStorage : window.sessionStorage;
      const probe = '__fd_probe__';
      s.setItem(probe, '1');
      s.removeItem(probe);
      return s;
    } catch {
      return null;
    }
  };
  return {
    get(key) {
      try {
        const s = backing();
        return s ? s.getItem(key) : (memory.get(key) ?? null);
      } catch {
        return memory.get(key) ?? null;
      }
    },
    set(key, value) {
      memory.set(key, value);
      try {
        backing()?.setItem(key, value);
      } catch {
        /* the memory copy is enough */
      }
    },
    remove(key) {
      memory.delete(key);
      try {
        backing()?.removeItem(key);
      } catch {
        /* ignore */
      }
    },
  };
}

export const localStore = createSafeStore('local');
export const sessionStore = createSafeStore('session');

const DEVICE_KEY = 'fd.device_id';
const TOKEN_KEY = 'fd.token';
const claimKey = (eventId: string) => `fd.claim_key.${eventId}`;

/** Random UUID persisted for this browser; sent as X-Device-Id on every request. */
export function getDeviceId(): string {
  const existing = localStore.get(DEVICE_KEY);
  if (existing) return existing;
  const fresh = uuidv4();
  localStore.set(DEVICE_KEY, fresh);
  return fresh;
}

export interface StoredSession {
  token: string;
  expires_at?: string;
  user_id?: string;
}

export function loadSession(): StoredSession | null {
  const raw = localStore.get(TOKEN_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as StoredSession;
    return typeof parsed.token === 'string' ? parsed : null;
  } catch {
    return null;
  }
}

export function saveSession(session: StoredSession): void {
  localStore.set(TOKEN_KEY, JSON.stringify(session));
}

export function clearSession(): void {
  localStore.remove(TOKEN_KEY);
}

/**
 * Idempotency-Key for a claim attempt. Created once, kept in sessionStorage
 * until the claim succeeds, so a refresh or a retry replays the same key and
 * the server can never allocate two seats.
 */
export function getClaimKey(eventId: string): string {
  const existing = sessionStore.get(claimKey(eventId));
  if (existing) return existing;
  const fresh = uuidv4();
  sessionStore.set(claimKey(eventId), fresh);
  return fresh;
}

export function clearClaimKey(eventId: string): void {
  sessionStore.remove(claimKey(eventId));
}
