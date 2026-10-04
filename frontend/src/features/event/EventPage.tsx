import { useEffect, useRef } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Fingerprint, Hourglass, Sparkles, Ticket as TicketIcon } from 'lucide-react';
import { api, describeError } from '@/api';
import { queryKeys } from '@/api/queryClient';
import type { EventInfo, Phase } from '@/api/schemas';
import { useAnnounce } from '@/app/Announcer';
import { pollInterval } from '@/api/polling';
import { formatCount, formatWindow } from '@/lib/format';
import { useDocumentTitle } from '@/lib/useDocumentTitle';
import { useSession } from '@/state/session';
import { Alert } from '@/ui/Alert';
import { PhaseBadge } from '@/ui/Badge';
import { Button, ButtonLink } from '@/ui/Button';
import { Skeleton } from '@/ui/Skeleton';
import { Ticket } from '@/ui/Ticket';
import { Countdown } from './Countdown';
import { EnterCard } from './EnterCard';
import { useDeadlineRefetch } from './useDeadlineRefetch';

const drawingPoll = pollInterval(6000, 4000);

const PHASE_ANNOUNCEMENT: Partial<Record<Phase, string>> = {
  OPEN: 'Entries are now open.',
  DRAWING: 'Entries have closed. The draw is running.',
  CLAIMING: 'The draw is done. Winners can claim their seats.',
  CLOSED: 'This drop is now closed.',
};

/** "9d2f4c1a…b07e55": enough to recognise, with the full value on the fairness page. */
function shortHash(h: string): string {
  return h.length > 18 ? `${h.slice(0, 8)}…${h.slice(-6)}` : h;
}

function minutes(seconds: number): string {
  const m = Math.round(seconds / 60);
  return m >= 1 ? `${m} minute${m === 1 ? '' : 's'}` : `${seconds} seconds`;
}

function Fact({ icon, label, children }: { icon: React.ReactNode; label: string; children: React.ReactNode }) {
  return (
    // A <dl> may only contain dt/dd (optionally wrapped in one div), so the icon lives inside the dt.
    <div>
      <dt className="flex items-center gap-2 text-sm text-ink-3">
        <span className="shrink-0" aria-hidden="true">
          {icon}
        </span>
        {label}
      </dt>
      <dd className="tnum pl-7 font-display text-base font-bold">{children}</dd>
    </div>
  );
}

function RushNote({ mode }: { mode: EventInfo['mode'] }) {
  if (mode === 'FCFS') {
    return (
      <Alert tone="warn" title="First come, first served">
        In this mode seats go to whoever claims first, so speed matters. It exists as the baseline to compare against the fair draw.
      </Alert>
    );
  }
  return (
    <Alert tone="success" title="No need to rush">
      Everyone who enters while the window is open has the same chance. Entering in the first second or the last one makes no difference.
    </Alert>
  );
}

function PageSkeleton() {
  return (
    <div className="space-y-6" aria-busy="true" aria-label="Loading drop">
      <Skeleton className="h-12 w-2/3" />
      <Skeleton className="h-40" />
      <Skeleton className="h-24" />
    </div>
  );
}

export function EventPage() {
  const { id = '' } = useParams();
  const session = useSession();
  const announce = useAnnounce();

  const eventQuery = useQuery({
    queryKey: queryKeys.event(id),
    queryFn: ({ signal }) => api.events.get(id, signal),
    enabled: id.length > 0,
    // While the draw is running, check back every 6 to 10 s (jittered). Paused when the tab is hidden.
    refetchInterval: (q) => (q.state.data?.phase === 'DRAWING' ? drawingPoll(q) : false),
  });
  const event = eventQuery.data;

  const statusQuery = useQuery({
    queryKey: queryKeys.status(id),
    queryFn: ({ signal }) => api.events.status(id, signal),
    enabled: !!session && id.length > 0,
  });

  useDocumentTitle(event?.name);

  // Ask the server what changed shortly after the next deadline (jittered), not at the exact instant.
  const deadline = event?.phase === 'SCHEDULED' ? event.window_opens_at : event?.phase === 'OPEN' ? event.window_closes_at : undefined;
  useDeadlineRefetch(deadline, eventQuery.refetch);

  // Tell assistive tech when the phase changes under the person (not on first load).
  const lastPhase = useRef<Phase | undefined>(undefined);
  useEffect(() => {
    const phase = event?.phase;
    if (phase && lastPhase.current && lastPhase.current !== phase) {
      const msg = PHASE_ANNOUNCEMENT[phase];
      if (msg) announce(msg);
    }
    lastPhase.current = phase;
  }, [event?.phase, announce]);

  if (eventQuery.isPending && !eventQuery.error) return <PageSkeleton />;

  if (eventQuery.error || !event) {
    const e = describeError(eventQuery.error);
    return (
      <div className="mx-auto max-w-xl space-y-4">
        <Alert
          tone={e.tone}
          title={e.title}
          action={
            e.code === 'NOT_FOUND' ? (
              <ButtonLink to="/" size="sm" variant="secondary">
                Back to the drops
              </ButtonLink>
            ) : (
              <Button size="sm" variant="secondary" onClick={() => void eventQuery.refetch()} loading={eventQuery.isFetching}>
                Try again
              </Button>
            )
          }
        >
          {e.body}
        </Alert>
      </div>
    );
  }

  const countdown =
    event.phase === 'SCHEDULED'
      ? { target: event.window_opens_at, label: 'Entries open in' }
      : event.phase === 'OPEN'
        ? { target: event.window_closes_at, label: 'Entries close in' }
        : null;

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <header className="space-y-3">
        <PhaseBadge phase={event.phase} />
        <h1 className="font-display text-3xl sm:text-5xl">{event.name}</h1>
        {event.description && <p className="max-w-prose text-lg text-ink-2">{event.description}</p>}
      </header>

      {countdown && <Countdown target={countdown.target} label={countdown.label} />}

      <RushNote mode={event.mode} />

      <Ticket
        tone={event.phase === 'OPEN' ? 'mint' : 'paper'}
        stub={
          <>
            <span className="flex items-center gap-1.5 text-sm text-ink-2">
              <Fingerprint className="size-4 shrink-0" aria-hidden="true" />
              <span className="hidden sm:inline">Draw commitment</span>
              <code className="hidden font-mono text-xs sm:inline">{event.seed_commitment ? shortHash(event.seed_commitment) : 'published at draw'}</code>
            </span>
            <Link to={`/events/${encodeURIComponent(event.id)}/fairness`} className="link whitespace-nowrap text-sm font-semibold">
              How we prove it’s fair
            </Link>
          </>
        }
      >
        <h2 className="mb-4 font-display text-xl">The details</h2>
        <dl className="grid gap-5 sm:grid-cols-3">
          <Fact icon={<TicketIcon className="size-5" />} label="Seats">
            {formatCount(event.inventory)}
          </Fact>
          <Fact icon={<Hourglass className="size-5" />} label="Entry window">
            {formatWindow(event.window_opens_at, event.window_closes_at)}
          </Fact>
          <Fact icon={<Sparkles className="size-5" />} label="If you’re picked">
            {minutes(event.claim_ttl_s)} to claim
          </Fact>
        </dl>
      </Ticket>

      <section aria-labelledby="enter-h" className="space-y-4">
        <h2 id="enter-h" className="font-display text-2xl">
          Your entry
        </h2>
        <EnterCard event={event} status={statusQuery.data} statusLoading={!!session && statusQuery.isPending && !statusQuery.error} />
      </section>
    </div>
  );
}
