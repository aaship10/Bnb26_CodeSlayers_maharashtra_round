import { useCallback, useEffect, useRef } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api, describeError } from '@/api';
import { queryKeys } from '@/api/queryClient';
import { useAnnounce } from '@/app/Announcer';
import { withJitter } from '@/lib/backoff';
import { useCountdownSeconds } from '@/lib/useCountdownSeconds';
import { useDocumentTitle } from '@/lib/useDocumentTitle';
import { useSession } from '@/state/session';
import { Alert } from '@/ui/Alert';
import { PhaseBadge } from '@/ui/Badge';
import { Skeleton } from '@/ui/Skeleton';
import { SignInPrompt } from '@/features/auth/SignInPrompt';
import { LiveIndicator } from '@/features/live/LiveIndicator';
import { isFinalStatus } from '@/features/live/LiveStatus';
import { useLiveStatus, type LiveState } from '@/features/live/useLiveStatus';
import { StatusCard } from './StatusCard';
import { VIEW_ANNOUNCEMENT, deriveStatusView, type StatusViewKind } from './statusView';

const time = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });

/** Shown above a known status when the connection is not healthy, so stale data is never mistaken for live data. */
function ConnectionBanner({ live }: { live: LiveState }) {
  const wait = useCountdownSeconds(live.retryInMs, live);
  const since = live.lastUpdated ? ` Showing what we knew at ${time.format(live.lastUpdated)}.` : '';
  if (live.connection === 'offline') {
    return (
      <Alert tone="offline" title="You’re offline">
        We’ll pick up where we left off as soon as you’re back.{since}
      </Alert>
    );
  }
  if (live.connection === 'reconnecting') {
    const rateLimited = live.error?.code === 'RATE_LIMITED';
    return (
      <Alert tone="offline" title={rateLimited ? 'Taking a short break from checking' : 'Reconnecting…'}>
        {rateLimited ? 'The server asked us to slow down.' : 'We lost the live connection.'}
        {wait > 0 ? ` Trying again in ${wait}s.` : ' Trying again now.'}
        {since}
      </Alert>
    );
  }
  return null;
}

export function StatusPage() {
  const { id = '' } = useParams();
  const session = useSession();
  const announce = useAnnounce();

  const eventQuery = useQuery({
    queryKey: queryKeys.event(id),
    queryFn: ({ signal }) => api.events.get(id, signal),
    enabled: !!id,
    staleTime: 60_000,
  });

  // One read for a fast first paint; after that LiveStatus keeps the same cache entry fresh.
  const statusQuery = useQuery({
    queryKey: queryKeys.status(id),
    queryFn: ({ signal }) => api.events.status(id, signal),
    enabled: !!session && !!id,
    staleTime: Infinity,
  });
  const status = statusQuery.data;
  const final = status ? isFinalStatus(status) : false;
  // Start listening once the first read has settled: a final result needs no connection at all,
  // and a failed read falls back to the stream (which retries politely).
  const live = useLiveStatus(id, !!session && !!id && !statusQuery.isPending && !final);

  useDocumentTitle('Your status');

  // A changed state is spoken; the first one isn't (the heading already says it).
  const lastView = useRef<StatusViewKind | null>(null);
  useEffect(() => {
    if (!status) return;
    const view = deriveStatusView(status);
    if (lastView.current && lastView.current !== view) announce(VIEW_ANNOUNCEMENT[view]);
    lastView.current = view;
  }, [status, announce]);

  // When the hold timer reaches zero, ask the server (it decides), after a small random lag.
  // If SSE is live the push will usually arrive first and this read is just a cheap confirmation.
  const refetchStatus = statusQuery.refetch;
  const onHoldElapsed = useCallback(() => {
    setTimeout(() => void refetchStatus(), withJitter(800, 3000));
  }, [refetchStatus]);

  if (!session) {
    return <SignInPrompt title="Sign in to see your status" body="Your entry and result are tied to your account, so we need to know it’s you." />;
  }

  const event = eventQuery.data;
  const phase = status?.phase ?? event?.phase;
  const problem = live.error ?? statusQuery.error;

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-2">
          {phase && <PhaseBadge phase={phase} />}
          <h1 className="font-display text-3xl sm:text-4xl">Your status</h1>
          {event && (
            <p className="text-ink-2">
              for{' '}
              <Link to={`/events/${encodeURIComponent(event.id)}`} className="link font-semibold">
                {event.name}
              </Link>
            </p>
          )}
        </div>
        <LiveIndicator connection={final ? 'final' : live.connection} />
      </header>

      {status && !final && <ConnectionBanner live={live} />}

      {status ? (
        <StatusCard status={status} event={event} onHoldElapsed={onHoldElapsed} />
      ) : problem ? (
        <NoStatus problem={problem} live={live} />
      ) : (
        <div aria-busy="true" aria-label="Loading your status">
          <Skeleton className="h-56" />
        </div>
      )}

      {live.error?.code === 'SCHEMA_MISMATCH' && (
        <Alert tone="error" title="We got an update we couldn’t read">
          What you see may be out of date. This is a bug on our side.
        </Alert>
      )}
    </div>
  );
}

/** No status to show at all: explain why, and when we'll try again. */
function NoStatus({ problem, live }: { problem: unknown; live: LiveState }) {
  const e = describeError(problem);
  const wait = useCountdownSeconds(live.retryInMs ?? e.retryAfterMs, live);
  const retrying = live.connection === 'reconnecting' || live.connection === 'polling' || live.connection === 'connecting';
  return (
    <Alert tone={e.tone} title={e.title}>
      {e.body}
      {retrying && (wait > 0 ? ` We’ll check again in ${wait}s.` : ' Checking again…')}
    </Alert>
  );
}
