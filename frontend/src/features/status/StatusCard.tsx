import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { Copy, Check } from 'lucide-react';
import type { EventInfo, StatusResponse } from '@/api/schemas';
import { formatCount, formatTime } from '@/lib/format';
import { Ball } from '@/ui/Ball';
import { ButtonLink } from '@/ui/Button';
import { Ticket } from '@/ui/Ticket';
import { Countdown } from '@/features/event/Countdown';
import { DrawingAnimation } from './DrawingAnimation';
import { HoldTimer } from './HoldTimer';
import { deriveStatusView } from './statusView';

interface Props {
  status: StatusResponse;
  event: EventInfo | undefined;
  onHoldElapsed?: () => void;
}

function Title({ children }: { children: ReactNode }) {
  return <h2 className="font-display text-2xl sm:text-3xl">{children}</h2>;
}

function Body({ children }: { children: ReactNode }) {
  return <p className="mt-2 max-w-prose text-ink-2">{children}</p>;
}

/** The person's pseudonymous draw id: lets them find themselves in the public draw results. */
function DrawId({ publicId, eventId }: { publicId: string; eventId: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(publicId); // unavailable on insecure origins; the id is still selectable
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* ignore */
    }
  };
  return (
    <div className="mt-5 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
      <span className="text-ink-3">Your draw ID</span>
      <code className="select-all rounded-sm bg-paper-3 px-1.5 py-0.5 font-mono text-xs">{publicId}</code>
      <button type="button" onClick={() => void copy()} className="inline-flex items-center gap-1 text-ink-2 hover:text-ink" aria-label="Copy draw ID">
        {copied ? <Check className="size-4 text-pine" aria-hidden="true" /> : <Copy className="size-4" aria-hidden="true" />}
        <span className="sr-only">{copied ? 'Copied' : ''}</span>
      </button>
      {/* passed in router state, never in the URL */}
      <Link to={`/events/${encodeURIComponent(eventId)}/fairness`} state={{ publicId }} className="link font-semibold">
        Find yourself in the draw
      </Link>
    </div>
  );
}

/**
 * One card per status. Copy is calm and specific: what happened, what (if anything)
 * to do, what happens next. Before the draw nothing hints at rank or odds.
 */
export function StatusCard({ status, event, onHoldElapsed }: Props) {
  const view = deriveStatusView(status);
  const eventId = event?.id ?? '';
  const eventPath = `/events/${encodeURIComponent(eventId)}`;
  const fairPath = `${eventPath}/fairness`;
  const closesAt = event ? formatTime(event.window_closes_at) : 'the end of the window';
  const drawId = status.public_id ? <DrawId publicId={status.public_id} eventId={eventId} /> : null;

  switch (view) {
    case 'not_open':
      return (
        <Ticket stub={<span className="text-sm text-ink-2">No need to be early</span>}>
          <Title>The window hasn’t opened yet</Title>
          <Body>Entering at the start or near the end gives exactly the same chance. Come back any time while it’s open.</Body>
          {event && <Countdown className="mt-5" target={event.window_opens_at} label="Entries open in" />}
          <div className="mt-5">
            <ButtonLink to={eventPath} variant="secondary">
              Go to the drop
            </ButtonLink>
          </div>
        </Ticket>
      );

    case 'not_entered':
      return (
        <Ticket tone="mint" stub={<span className="text-sm text-ink-2">Open until {closesAt}</span>}>
          <Title>You haven’t entered yet</Title>
          <Body>The window is open. Entering takes one tap, and there’s no advantage to doing it early.</Body>
          <div className="mt-5">
            <ButtonLink to={eventPath} size="lg">
              Enter now
            </ButtonLink>
          </div>
        </Ticket>
      );

    case 'waiting':
      return (
        <Ticket tone="mint" stub={<span className="text-sm text-ink-2">Nothing more to do</span>}>
          <Title>You’re in the draw</Title>
          <Body>
            When entries close at {closesAt}, one public draw picks the seats. Until then nobody has a position, not you and not anyone
            else, so there’s nothing to watch and nothing to refresh.
          </Body>
          {event && <Countdown className="mt-5" target={event.window_closes_at} label="Entries close in" />}
        </Ticket>
      );

    case 'drawing':
      return (
        <Ticket tone="sun" stub={<span className="text-sm text-ink-2">This page updates by itself</span>}>
          <div className="flex flex-col items-center gap-6 py-2 text-center sm:flex-row sm:text-left">
            <DrawingAnimation />
            <div>
              <Title>The draw is running</Title>
              <Body>Every entry has the same chance. Your result will appear here in a few seconds, with no need to refresh.</Body>
            </div>
          </div>
        </Ticket>
      );

    case 'won':
      return (
        <Ticket tone="sun" stub={<span className="text-sm text-ink-2">If the timer runs out, the seat passes to the next person on the waitlist.</span>}>
          <Title>You’ve been picked!</Title>
          <Body>A seat is being held for you. Claim it before the timer runs out.</Body>
          {status.hold_expires_at && <HoldTimer className="mt-5" expiresAt={status.hold_expires_at} onElapsed={onHoldElapsed} />}
          <div className="mt-5">
            <ButtonLink to={`${eventPath}/claim`} size="lg">
              Claim my seat
            </ButtonLink>
          </div>
          {drawId}
        </Ticket>
      );

    case 'waitlisted':
      return (
        <Ticket stub={<span className="text-sm text-ink-2">This page updates by itself</span>}>
          <div className="flex items-start gap-4">
            {status.waitlist_position !== undefined && (
              <Ball n={status.waitlist_position} color="cobalt" className="size-16 shrink-0" label={`Waitlist position ${status.waitlist_position}`} />
            )}
            <div>
              <Title>You’re on the waitlist</Title>
              {status.waitlist_position !== undefined ? (
                <Body>
                  You’re number {formatCount(status.waitlist_position)} in line. If someone picked doesn’t claim in time, their seat passes
                  down the waitlist in order.
                </Body>
              ) : (
                <Body>If someone picked doesn’t claim in time, their seat passes down the waitlist in order.</Body>
              )}
              <Body>If your turn comes, this page will show it and give you time to claim.</Body>
            </div>
          </div>
          {drawId}
        </Ticket>
      );

    case 'lost':
      return (
        <Ticket stub={<Link to={fairPath} className="link text-sm font-semibold">See how the draw was made</Link>}>
          <Title>Not this time</Title>
          <Body>
            The draw didn’t pick you, and claiming closed before the waitlist reached you. Every entry had the same chance, and you can
            check the draw yourself.
          </Body>
          {drawId}
        </Ticket>
      );

    case 'claimed':
      return (
        <Ticket
          tone="mint"
          stub={
            <>
              <span className="text-sm text-ink-2">Keep this ticket</span>
              <ButtonLink to={`${eventPath}/ticket`} size="sm">
                View ticket
              </ButtonLink>
            </>
          }
        >
          <div className="flex items-start gap-4">
            {status.seat_no !== undefined && <Ball n={status.seat_no} color="mint" className="size-16 shrink-0" label={`Seat ${status.seat_no}`} />}
            <div>
              <Title>Your seat is confirmed</Title>
              <Body>
                {status.seat_no !== undefined ? `Seat ${status.seat_no} is yours.` : 'Your seat is yours.'} Your ticket is saved to your account,
                so you can come back to it any time.
              </Body>
            </div>
          </div>
          {drawId}
        </Ticket>
      );

    case 'expired':
      return (
        <Ticket stub={<Link to={fairPath} className="link text-sm font-semibold">See how the draw was made</Link>}>
          <Title>Your hold ran out</Title>
          <Body>The time to claim passed, so the seat moved on to the next person waiting. Thanks for taking part.</Body>
          {drawId}
        </Ticket>
      );

    case 'missed':
      return (
        <Ticket stub={<Link to="/" className="link text-sm font-semibold">See other drops</Link>}>
          <Title>You didn’t enter this drop</Title>
          <Body>Entries closed before you entered, so you weren’t part of the draw. Keep an eye out for the next one.</Body>
        </Ticket>
      );
  }
}
