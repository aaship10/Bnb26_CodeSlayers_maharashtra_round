import { useCallback, useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '@/api';
import { ApiError, isApiError } from '@/api/errors';
import { queryKeys } from '@/api/queryClient';
import { isTransient, retryTransient } from '@/api/retryTransient';
import type { ClaimResponse, StatusResponse } from '@/api/schemas';
import { useAnnounce } from '@/app/Announcer';
import { withChallenges, type ChallengeHandler } from '@/features/challenge/withChallenge';
import { clearClaimKey, getClaimKey, peekClaimKey } from '@/lib/storage';
import { serverClock } from '@/lib/serverClock';

export type ClaimPhase = 'idle' | 'sending' | 'retrying' | 'done';

export interface UseClaim {
  phase: ClaimPhase;
  error: ApiError | null;
  /** True when the last attempt failed in a way that leaves the outcome unknown (network/timeout/5xx). */
  uncertain: boolean;
  /** A claim was started in this tab (e.g. before a refresh) and not confirmed yet. */
  resumable: boolean;
  result: ClaimResponse | null;
  claim: () => Promise<void>;
  cancel: () => void;
}

export const claimResultKey = (eventId: string) => ['events', eventId, 'claim'] as const;

const isAbort = (e: unknown) => e instanceof DOMException && e.name === 'AbortError';

/**
 * Claiming a held seat, safely repeatable.
 *
 * The Idempotency-Key is created the first time the person taps Claim and kept
 * in sessionStorage until the server confirms. Every retry (automatic after a
 * network blip, or a tap after a refresh) sends the SAME key, so however many
 * times the request reaches the server it can allocate at most one seat.
 * The key is dropped only when the attempt is definitively over (claimed,
 * already claimed, hold expired, not a winner).
 */
export function useClaim(eventId: string, handler: ChallengeHandler, onClaimed: (r: ClaimResponse) => void): UseClaim {
  const queryClient = useQueryClient();
  const announce = useAnnounce();
  const [phase, setPhase] = useState<ClaimPhase>('idle');
  const [error, setError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<ClaimResponse | null>(null);
  const [resumable, setResumable] = useState(() => peekClaimKey(eventId) !== null);
  const controller = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  const onClaimedRef = useRef(onClaimed);
  onClaimedRef.current = onClaimed;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      controller.current?.abort();
    };
  }, []);

  const recordClaimed = useCallback(
    (r: ClaimResponse) => {
      clearClaimKey(eventId);
      queryClient.setQueryData(claimResultKey(eventId), r);
      queryClient.setQueryData<StatusResponse>(queryKeys.status(eventId), (prev) => ({
        ...(prev ?? { phase: 'CLAIMING' as const }),
        state: 'CLAIMED',
        seat_no: r.seat_no,
        ticket_code: r.ticket_code,
        hold_expires_at: undefined,
        server_now: prev?.server_now ?? new Date(serverClock.now()).toISOString(),
      }));
    },
    [eventId, queryClient],
  );

  const claim = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    const key = getClaimKey(eventId); // reused if one is already pending
    setResumable(true);
    setError(null);
    setPhase('sending');
    const ctrl = new AbortController();
    controller.current = ctrl;

    try {
      const res = await withChallenges(
        (solution) =>
          retryTransient(() => api.events.claim(eventId, key, solution, ctrl.signal), {
            retries: 3,
            signal: ctrl.signal,
            onRetry: () => mounted.current && setPhase('retrying'),
          }),
        handler,
        { signal: ctrl.signal },
      );
      recordClaimed(res);
      if (!mounted.current) return;
      setResult(res);
      setResumable(false);
      setPhase('done');
      announce(`Seat ${res.seat_no} is yours.`);
      onClaimedRef.current(res);
    } catch (e) {
      if (!mounted.current || ctrl.signal.aborted || isAbort(e)) {
        if (mounted.current) setPhase('idle');
        return;
      }
      const err = isApiError(e) ? e : new ApiError('UNKNOWN', String(e), 0, undefined, undefined, e);

      if (err.code === 'ALREADY_CLAIMED') {
        // An earlier attempt (maybe from before a refresh) got there. The server has the ticket; go get it.
        clearClaimKey(eventId);
        setResumable(false);
        const status = await queryClient.fetchQuery({
          queryKey: queryKeys.status(eventId),
          queryFn: ({ signal }) => api.events.status(eventId, signal),
          staleTime: 0,
        }).catch(() => undefined);
        if (status?.state === 'CLAIMED' && status.seat_no && status.ticket_code) {
          const r: ClaimResponse = { state: 'CLAIMED', seat_no: status.seat_no, ticket_code: status.ticket_code };
          recordClaimed(r);
          if (mounted.current) {
            setResult(r);
            setPhase('done');
            onClaimedRef.current(r);
          }
          return;
        }
      }

      if (err.code === 'HOLD_EXPIRED' || err.code === 'NOT_WINNER' || err.code === 'ALREADY_CLAIMED') {
        clearClaimKey(eventId); // this attempt is over; nothing to resume
        setResumable(false);
        void queryClient.invalidateQueries({ queryKey: queryKeys.status(eventId), exact: true });
      }
      setError(err);
      setPhase('idle');
    } finally {
      inFlight.current = false;
    }
  }, [eventId, handler, announce, queryClient, recordClaimed]);

  const cancel = useCallback(() => controller.current?.abort(), []);

  return { phase, error, uncertain: !!error && isTransient(error), resumable, result, claim, cancel };
}
