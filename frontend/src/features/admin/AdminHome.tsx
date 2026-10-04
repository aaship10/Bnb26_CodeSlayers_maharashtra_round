import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Plus } from 'lucide-react';
import { describeError } from '@/api/errors';
import { formatCount, formatWindow } from '@/lib/format';
import { Alert } from '@/ui/Alert';
import { PhaseBadge } from '@/ui/Badge';
import { Button } from '@/ui/Button';
import { Skeleton } from '@/ui/Skeleton';
import { adminApi, adminKeys } from './adminApi';
import { AdminShell } from './AdminShell';
import { CreateEventDialog } from './CreateEventDialog';

function EventList() {
  const events = useQuery({ queryKey: adminKeys.events, queryFn: ({ signal }) => adminApi.events(signal) });

  if (events.isPending) return <Skeleton className="h-48" />;
  if (events.error) {
    const e = describeError(events.error);
    return (
      <Alert tone={e.tone} title={e.title}>
        {e.body}
      </Alert>
    );
  }
  if (events.data.length === 0) return <Alert title="No events yet">Create one to get started.</Alert>;

  return (
    <ul className="divide-y-2 divide-rule overflow-hidden rounded-lg border-2 border-ink bg-paper-2">
      {events.data.map((ev) => (
        <li key={ev.id}>
          <Link
            to={`/admin/events/${encodeURIComponent(ev.id)}`}
            className="grid gap-x-6 gap-y-1 px-4 py-3 hover:bg-sun-tint focus-visible:bg-sun-tint sm:grid-cols-[1fr_auto_auto] sm:items-center"
          >
            <span className="min-w-0">
              <span className="block truncate font-display text-lg font-bold">{ev.name}</span>
              <span className="block text-sm text-ink-3">
                {formatWindow(ev.window_opens_at, ev.window_closes_at)} · {ev.mode === 'LOTTERY' ? 'Fair draw' : 'First come, first served'}
              </span>
            </span>
            <span className="tnum text-sm text-ink-2">{formatCount(ev.inventory)} seats</span>
            <PhaseBadge phase={ev.phase} />
          </Link>
        </li>
      ))}
    </ul>
  );
}

export function Component() {
  const [creating, setCreating] = useState(false);
  return (
    <AdminShell title="Events">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-3xl">Events</h1>
          <p className="text-ink-2">Run the lifecycle, tune defences and watch the numbers.</p>
        </div>
        <Button onClick={() => setCreating(true)} leading={<Plus className="size-5" aria-hidden="true" />}>
          New event
        </Button>
      </div>
      <EventList />
      <CreateEventDialog open={creating} onClose={() => setCreating(false)} />
    </AdminShell>
  );
}
