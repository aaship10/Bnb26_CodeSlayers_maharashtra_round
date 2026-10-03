import { CalendarClock } from 'lucide-react';
import type { EventInfo } from '@/api/schemas';
import { PhaseBadge } from '@/ui/Badge';
import { ButtonLink } from '@/ui/Button';
import { Ticket } from '@/ui/Ticket';
import { formatCount, formatWindow } from '@/lib/format';

const MODE_LABEL: Record<EventInfo['mode'], string> = {
  LOTTERY: 'Fair draw',
  FCFS: 'First come, first served',
};

/** An event as a ticket: facts on the front, phase and call-to-action on the stub. */
export function EventTicket({ event }: { event: EventInfo }) {
  const tone = event.phase === 'OPEN' ? 'mint' : event.phase === 'CLAIMING' || event.phase === 'DRAWING' ? 'sun' : 'paper';
  return (
    <Ticket
      tone={tone}
      stub={
        <>
          <PhaseBadge phase={event.phase} />
          <ButtonLink to={`/events/${encodeURIComponent(event.id)}`} size="sm" variant={event.phase === 'OPEN' ? 'primary' : 'secondary'}>
            View drop
          </ButtonLink>
        </>
      }
    >
      <p className="font-mono text-xs font-semibold uppercase tracking-widest text-ink-3">{MODE_LABEL[event.mode]}</p>
      <h3 className="mt-1 font-display text-xl sm:text-2xl">{event.name}</h3>
      {event.description && <p className="mt-2 text-ink-2">{event.description}</p>}
      <dl className="mt-4 grid grid-cols-[auto_1fr] items-baseline gap-x-4 gap-y-1 text-sm">
        <dt className="text-ink-3">Seats</dt>
        <dd className="tnum font-display text-base font-bold">{formatCount(event.inventory)}</dd>
        <dt className="flex items-center gap-1.5 text-ink-3">
          <CalendarClock className="size-4" aria-hidden="true" />
          Window
        </dt>
        <dd className="tnum">
          {formatWindow(event.window_opens_at, event.window_closes_at)}
        </dd>
      </dl>
    </Ticket>
  );
}
