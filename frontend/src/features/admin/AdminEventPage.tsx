import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, RotateCcw } from 'lucide-react';
import { describeError } from '@/api/errors';
import { formatCount, formatWindow } from '@/lib/format';
import { uuidv4 } from '@/lib/ids';
import { Alert } from '@/ui/Alert';
import { PhaseBadge } from '@/ui/Badge';
import { Button } from '@/ui/Button';
import { ConfirmDialog } from '@/ui/ConfirmDialog';
import { Skeleton } from '@/ui/Skeleton';
import { adminApi, adminKeys } from './adminApi';
import { AdminShell } from './AdminShell';
import { DefencePanel } from './DefencePanel';
import { InvariantsBadge, invariantsHold, invariantsSummary, useInvariants } from './Invariants';
import { pollInterval } from './logic';
import { PhaseTimeline } from './PhaseTimeline';
import type { AdminEvent } from './schemas';
import { StatsPanel, useStats } from './StatsPanel';

/** Reset exists for rehearsals only. It is compiled out of production builds unless explicitly enabled. */
export const RESET_ENABLED = import.meta.env.DEV || import.meta.env.VITE_ENABLE_RESET === 'true';

function DangerZone({ event }: { event: AdminEvent }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState('');
  const reset = async () => {
    const updated = await adminApi.reset(event.id, key);
    queryClient.setQueryData(adminKeys.event(event.id), updated);
    void queryClient.invalidateQueries({ queryKey: adminKeys.all });
  };
  return (
    <section aria-labelledby="danger-h" className="rounded-lg border-2 border-dashed border-tomato-deep bg-tomato-tint/40 p-4 sm:p-5">
      <h2 id="danger-h" className="font-display text-xl text-tomato-deep">
        Rehearsal reset
      </h2>
      <p className="mt-1 text-sm text-ink-2">Development only. Clears every entry, hold and claim for this event and returns it to Draft.</p>
      <Button
        className="mt-3"
        variant="secondary"
        size="sm"
        leading={<RotateCcw className="size-4" aria-hidden="true" />}
        onClick={() => {
          setKey(uuidv4());
          setOpen(true);
        }}
      >
        Reset this event…
      </Button>
      <ConfirmDialog open={open} danger title={`Reset “${event.name}”?`} confirmLabel="Reset everything" requireText="RESET" onConfirm={reset} onClose={() => setOpen(false)}>
        Every entry, hold and claim for this event will be deleted and it goes back to Draft. Attendees lose their places. Use this only
        between demo runs.
      </ConfirmDialog>
    </section>
  );
}

function EventBody({ id }: { id: string }) {
  const event = useQuery({
    queryKey: adminKeys.event(id),
    queryFn: ({ signal }) => adminApi.event(id, signal),
    // the phase can also move by itself (scheduled times, holds running out)
    refetchInterval: pollInterval(10_000, 5_000),
    retry: false,
  });
  const stats = useStats(id);
  const invariants = useInvariants(id);

  if (event.isPending) return <Skeleton className="h-96" />;
  if (event.error) {
    const e = describeError(event.error);
    return (
      <Alert tone={e.tone} title={e.title}>
        {e.body}
      </Alert>
    );
  }

  const ev = event.data;
  const broken = invariants.data && !invariantsHold(invariants.data);

  return (
    <div className="space-y-6">
      <Link to="/admin" className="inline-flex items-center gap-1 text-sm font-semibold text-ink-2 hover:text-ink">
        <ArrowLeft className="size-4" aria-hidden="true" /> All events
      </Link>

      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-2">
          <PhaseBadge phase={ev.phase} />
          <h1 className="font-display text-3xl">{ev.name}</h1>
          <p className="text-sm text-ink-2">
            {ev.mode === 'LOTTERY' ? 'Fair draw' : 'First come, first served'} · {formatCount(ev.inventory)} seats · window{' '}
            {formatWindow(ev.window_opens_at, ev.window_closes_at)} · {Math.round(ev.claim_ttl_s / 60)} min to claim
          </p>
          <p className="font-mono text-xs text-ink-3">{ev.id}</p>
        </div>
        <InvariantsBadge query={invariants} />
      </header>

      {broken && invariants.data && (
        <Alert tone="error" title="An allocation invariant is violated">
          {invariantsSummary(invariants.data)} (duplicate users {invariants.data.duplicate_users}, duplicate seats {invariants.data.duplicate_seats}). Pause the
          event and investigate before running anything else.
        </Alert>
      )}

      <PhaseTimeline event={ev} entrants={stats.data?.entrants} />

      <div className="grid items-start gap-6 lg:grid-cols-[1.3fr_1fr]">
        <StatsPanel query={stats} />
        <DefencePanel event={ev} />
      </div>

      {RESET_ENABLED && <DangerZone event={ev} />}
    </div>
  );
}

export function Component() {
  const { id = '' } = useParams();
  return (
    <AdminShell title="Event">
      <EventBody id={id} />
    </AdminShell>
  );
}
