import { useSyncExternalStore } from 'react';
import { clearSession, loadSession, saveSession, type StoredSession } from '@/lib/storage';

/**
 * Tiny external store for the signed-in session. Only the token (and its expiry
 * and user id) is kept client-side; everything else comes from the server.
 */
type Listener = () => void;

let current: StoredSession | null = loadSession();
const listeners = new Set<Listener>();

function emit() {
  listeners.forEach((l) => l());
}

export const sessionStore = {
  getSnapshot: (): StoredSession | null => current,
  subscribe(listener: Listener): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
  set(session: StoredSession) {
    current = session;
    saveSession(session);
    emit();
  },
  clear() {
    current = null;
    clearSession();
    emit();
  },
};

export function useSession(): StoredSession | null {
  return useSyncExternalStore(sessionStore.subscribe, sessionStore.getSnapshot, () => null);
}
