import { useEffect } from 'react';
import { api } from '@/api';
import { isApiError } from '@/api/errors';
import { serverClock } from '@/lib/serverClock';
import { withJitter } from '@/lib/backoff';
import { sessionStore, useSession } from '@/state/session';

const REFRESH_BEFORE_EXPIRY_MS = 5 * 60_000;
const MAX_TIMEOUT_MS = 2 ** 31 - 1;

/**
 * Refreshes the token a few minutes before it expires, with jitter so tabs that
 * were all opened together don't refresh in the same second. Failure is quiet:
 * the next real request will surface UNAUTHENTICATED and send the person to sign in.
 */
export function useSessionKeepAlive(): void {
  const session = useSession();
  const token = session?.token;
  const expiresAt = session?.expires_at;

  useEffect(() => {
    if (!token || !expiresAt) return;
    const ms = serverClock.msUntil(expiresAt) - REFRESH_BEFORE_EXPIRY_MS;
    const delay = Math.min(MAX_TIMEOUT_MS, withJitter(Math.max(0, ms), 30_000));
    const ctrl = new AbortController();

    const timer = setTimeout(() => {
      api.auth
        .refresh(ctrl.signal)
        .then((res) => sessionStore.set({ token: res.token, expires_at: res.expires_at, user_id: res.user_id }))
        .catch((e: unknown) => {
          if (isApiError(e) && e.code === 'UNAUTHENTICATED') sessionStore.clear();
        });
    }, delay);

    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [token, expiresAt]);
}
