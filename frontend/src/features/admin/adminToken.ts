import { useSyncExternalStore } from 'react';
import { sessionStore } from '@/lib/storage';

/**
 * The organizer's admin token. sessionStorage ONLY: it dies with the tab, never
 * touches localStorage, and is never put in a URL. Sent as X-Admin-Token.
 */
const KEY = 'fd.admin_token';
type Listener = () => void;
const listeners = new Set<Listener>();
let lastReason: string | null = null;

export const adminToken = {
  get: (): string | null => sessionStore.get(KEY),
  set(token: string) {
    lastReason = null;
    sessionStore.set(KEY, token);
    listeners.forEach((l) => l());
  },
  /** Forget the token; `reason` is shown on the unlock dialog ("That token wasn't accepted"). */
  clear(reason: string | null = null) {
    lastReason = reason;
    sessionStore.remove(KEY);
    listeners.forEach((l) => l());
  },
  get reason(): string | null {
    return lastReason;
  },
  subscribe(l: Listener) {
    listeners.add(l);
    return () => listeners.delete(l);
  },
};

export function useAdminToken(): string | null {
  return useSyncExternalStore(adminToken.subscribe, adminToken.get, () => null);
}
