import { useCallback } from 'react';
import { Navigate, useNavigate, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ShieldCheck } from 'lucide-react';
import { api, describeError } from '@/api';
import { queryKeys } from '@/api/queryClient';
import { withJitter } from '@/lib/backoff';
import { useCountdownSeconds } from '@/lib/useCountdownSeconds';
import { useDocumentTitle } from '@/lib/useDocumentTitle';
import { useSession } from '@/state/session';
import { Alert } from '@/ui/Alert';
import { Button, ButtonLink } from '@/ui/Button';
import { Skeleton } from '@/ui/Skeleton';
import { Ticket } from '@/ui/Ticket';
import { SignInPrompt } from '@/features/auth/SignInPrompt';
import { ChallengePanel } from '@/features/challenge/ChallengePanel';
import { useChallenge } from '@/features/challenge/useChallenge';
import { LiveIndicator } from '@/features/live/LiveIndicator';
import { isFinalStatus } from '@/features/live/LiveStatus';
import { useLiveStatus } from '@/features/live/useLiveStatus';
import { HoldTimer } from '@/features/status/HoldTimer';
import { useClaim } from './useClaim';

export function ClaimPage() {
  const { id = '' } = useParams();
  const session = useSession();
  const navigate = useNavigate();
  useDocumentTitle('Claim your seat');

  const eventQuery = useQuery({ queryKey: queryKeys.event(id), queryFn: ({ signal }) => api.events.get(id, signal), enabled: !!id, staleTime: 60_000 });
  const statusQuery = useQuery({
    queryKey: queryKeys.status(id),
    queryFn: ({ signal }) => api.events.status(id, signal),
    enabled: !!session && !!id,
    staleTime: Infinity,
  });
  const status = statusQuery.data;
  const live = useLiveStatus(id, !!session && !!id && !statusQuery.isPending && !(status && isFinalStatus(status)));

  const challenge = useChallenge();
  const toTicket = useCallback(
    () => navigate(`/events/${encodeURIComponent(id)}/ticket`, { replace: true, state: { justClaimed: true } }),
    [navigate, id],
  );
  const claim = useClaim(id, challenge.handler, toTicket);

  const err = claim.error ? describeError(claim.error) : null;
  const waitS = useCountdownSeconds(err?.code === 'RATE_LIMITED' ? err.retryAfterMs : undefined, claim.error);
  const refetch = statusQuery.refetch;
  const onHoldElapsed = useCallback(() => setTimeout(() => void refetch(), withJitter(800, 3000)), [refetch]);

  if (!session) return <SignInPrompt title="Sign in to claim your seat" body="Your held seat is tied to your account." />;

  if (!status) {
    if (statusQuery.error) {
      const e = describeError(statusQuery.error);
      return (
        <div className="mx-auto max-w-xl">
          <Alert tone={e.tone} title={e.title}>
            {e.body}
          </Alert>
        </div>
      );
    }
    return <Skeleton className="mx-auto h-72 max-w-xl" />;
  }

  // Already done (maybe in another tab, or before a refresh): the ticket is the answer.
  if (status.state === 'CLAIMED' && claim.phase === 'idle') {
    return <Navigate to={`/events/${encodeURIComponent(id)}/ticket`} replace />;
  }

  const statusPath = `/events/${encodeURIComponent(id)}/status`;

  if (status.state !== 'WON') {
    return (
      <div className="mx-auto max-w-xl space-y-4">
        {err ? (
          <Alert tone={err.tone} title={err.title}>
            {err.body}
          </Alert>
        ) : (
          <Alert title="There’s no seat to claim right now">Seats are held only for people picked in the draw.</Alert>
        )}
        <ButtonLink to={statusPath} variant="secondary">
          See my status
        </ButtonLink>
      </div>
    );
  }

  const busy = claim.phase === 'sending' || claim.phase === 'retrying';
  const blocked = err?.code === 'RATE_LIMITED' && waitS > 0;
  const challenging = challenge.state.kind !== 'idle';
  const label =
    claim.phase === 'retrying'
      ? 'Still trying…'
      : busy
        ? 'Claiming…'
        : blocked
          ? `Try again in ${waitS}s`
          : err?.code === 'RATE_LIMITED'
            ? 'Try again'
            : claim.uncertain || claim.resumable
            ? 'Finish claiming'
            : 'Claim my seat';

  return (
    <div className="mx-auto max-w-xl space-y-6">
      <div className="flex items-center justify-between gap-3">
        <h1 className="font-display text-3xl sm:text-4xl">Claim your seat</h1>
        <LiveIndicator connection={live.connection} />
      </div>

      <Ticket
        tone="sun"
        stub={
          <span className="flex items-center gap-2 text-sm text-ink-2">
            <ShieldCheck className="size-4 shrink-0" aria-hidden="true" />
            Safe to tap again: you can never end up with two seats.
          </span>
        }
      >
        {eventQuery.data && <p className="font-mono text-xs font-semibold uppercase tracking-widest text-ink-3">{eventQuery.data.name}</p>}
        <h2 className="mt-1 font-display text-2xl">One seat, held for you</h2>
        {status.hold_expires_at && <HoldTimer className="mt-5" expiresAt={status.hold_expires_at} onElapsed={onHoldElapsed} />}

        <div className="mt-6 space-y-4">
          {!challenging && (
            <Button size="lg" className="w-full" onClick={() => void claim.claim()} loading={busy} disabled={blocked}>
              {label}
            </Button>
          )}
          <ChallengePanel state={challenge.state} onCaptcha={challenge.submitCaptcha} onCancel={claim.cancel} />

          {claim.phase === 'retrying' && (
            <p role="status" className="text-sm text-ink-2">
              Your connection hiccupped. Retrying the same claim, so it can only count once.
            </p>
          )}

          {claim.resumable && !busy && !err && (
            <Alert title="You started claiming earlier">
              We didn’t hear back last time. Tap the button to finish; it’s the same claim, not a new one.
            </Alert>
          )}

          {err &&
            (claim.uncertain ? (
              <Alert tone="warn" title="We’re not sure your claim went through">
                The connection dropped before the server answered. Tap “Finish claiming”: it repeats the same claim, so you’ll get your
                seat once, not twice.
              </Alert>
            ) : (
              <Alert tone={err.tone} title={err.title}>
                {blocked ? `You can try again in ${waitS} second${waitS === 1 ? '' : 's'}.` : err.body}
              </Alert>
            ))}
        </div>
      </Ticket>
    </div>
  );
}
