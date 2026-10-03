import { useCallback, useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '@/api';
import { ApiError, isApiError } from '@/api/errors';
import { queryKeys } from '@/api/queryClient';
import { retryTransient } from '@/api/retryTransient';
import type { EnterResponse } from '@/api/schemas';
import { useAnnounce } from '@/app/Announcer';
import { withChallenges, type ChallengeHandler } from '@/features/challenge/withChallenge';

export type EnterPhase = 'idle' | 'sending' | 'retrying' | 'done';

export interface UseEnter {
  phase: EnterPhase;
  error: ApiError | null;
  result: EnterResponse | null;
  enter: () => Promise<void>;
  clearError: () => void;
  /** Abort the request and any challenge in progress. */
  cancel: () => void;
}

function toApiError(e: unknown): ApiError {
  if (isApiError(e)) return e;
  return new ApiError('UNKNOWN', e instanceof Error ? e.message : String(e), 0, undefined, undefined, e);
}

const isAbort = (e: unknown) => e instanceof DOMException && e.name === 'AbortError';

/**
 * One tap to enter. Entering is idempotent on the server, so the safe-to-repeat
 * failures (dropped connection, timeout, 5xx) are retried automatically with
 * jittered backoff. A 429 is NOT retried automatically: we show the countdown
 * and let the person tap again, so a rate-limited crowd doesn't re-fire in sync.
 * A CHALLENGE_REQUIRED is solved and the same request is repeated once.
 */
export function useEnter(eventId: string, handler: ChallengeHandler): UseEnter {
  const queryClient = useQueryClient();
  const announce = useAnnounce();
  const [phase, setPhase] = useState<EnterPhase>('idle');
  const [error, setError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<EnterResponse | null>(null);
  const controller = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      controller.current?.abort(); // leaving the page cancels the worker and any pending request
    };
  }, []);

  const enter = useCallback(async () => {
    if (inFlight.current) return; // a second tap while pending does nothing
    inFlight.current = true;
    setError(null);
    setPhase('sending');
    const ctrl = new AbortController();
    controller.current = ctrl;

    try {
      const res = await withChallenges(
        (solution) =>
          retryTransient(() => api.events.enter(eventId, solution, ctrl.signal), {
            signal: ctrl.signal,
            onRetry: () => mounted.current && setPhase('retrying'),
          }),
        handler,
        { signal: ctrl.signal },
      );
      if (!mounted.current) return;
      setResult(res);
      setPhase('done');
      announce(res.already_entered ? 'You had already entered. Your entry is safe.' : 'You are entered in the draw.');
      void queryClient.invalidateQueries({ queryKey: queryKeys.status(eventId) });
    } catch (e) {
      if (!mounted.current || ctrl.signal.aborted || isAbort(e)) {
        if (mounted.current) setPhase('idle');
        return;
      }
      const err = toApiError(e);
      setError(err);
      setPhase('idle');
      if (err.code === 'WINDOW_CLOSED' || err.code === 'WINDOW_NOT_OPEN') {
        // The page's idea of the phase is out of date; ask the server.
        void queryClient.invalidateQueries({ queryKey: queryKeys.event(eventId), exact: true });
      }
    } finally {
      inFlight.current = false;
    }
  }, [eventId, handler, announce, queryClient]);

  const cancel = useCallback(() => controller.current?.abort(), []);
  const clearError = useCallback(() => setError(null), []);

  return { phase, error, result, enter, clearError, cancel };
}
