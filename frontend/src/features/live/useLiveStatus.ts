import { useEffect, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api, apiClient } from '@/api';
import { queryKeys } from '@/api/queryClient';
import { sessionStore } from '@/state/session';
import { LiveStatus, type ConnectionInfo } from './LiveStatus';
import { streamSse } from './streamSse';

export interface LiveState extends ConnectionInfo {
  /** Local time of the last fresh status, for "last updated" when the connection is down. */
  lastUpdated: number | null;
}

/**
 * Keeps the status query for this event up to date through LiveStatus (SSE first,
 * polite polling fallback). Pauses while the tab is hidden or the device is offline.
 * Updates land in the TanStack Query cache, so every component reading the status
 * query re-renders; nothing here stores state in the browser.
 */
export function useLiveStatus(eventId: string, enabled: boolean): LiveState {
  const queryClient = useQueryClient();
  const [info, setInfo] = useState<ConnectionInfo>({ connection: enabled ? 'connecting' : 'stopped' });
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);

  useEffect(() => {
    if (!enabled || !eventId) {
      setInfo({ connection: 'stopped' });
      return;
    }

    const url = `${apiClient.baseUrl}/events/${encodeURIComponent(eventId)}/stream`;
    const live = new LiveStatus(
      {
        // Headers are read per attempt so a refreshed token is picked up on reconnect.
        stream: (args) => streamSse({ url, headers: apiClient.identityHeaders(), ...args }),
        poll: (signal) => api.events.status(eventId, signal),
      },
      {
        onStatus: (status) => {
          queryClient.setQueryData(queryKeys.status(eventId), status);
          setLastUpdated(Date.now());
        },
        onConnection: (next) => {
          setInfo(next);
          if (next.error?.code === 'UNAUTHENTICATED') sessionStore.clear();
        },
      },
    );

    live.start();

    const onVisibility = () => (document.visibilityState === 'hidden' ? live.suspend('paused') : live.resume());
    const onOffline = () => live.suspend('offline');
    const onOnline = () => live.resume();
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('offline', onOffline);
    window.addEventListener('online', onOnline);
    if (document.visibilityState === 'hidden') live.suspend('paused');
    else if (navigator.onLine === false) live.suspend('offline');

    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('offline', onOffline);
      window.removeEventListener('online', onOnline);
      live.stop();
    };
  }, [eventId, enabled, queryClient]);

  // Stable identity per connection change, so consumers can key countdowns on it.
  return useMemo(() => ({ ...info, lastUpdated }), [info, lastUpdated]);
}
