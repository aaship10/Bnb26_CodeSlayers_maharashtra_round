import { useEffect, useRef } from 'react';
import { useAnnounce } from '@/app/Announcer';
import { splitDuration } from '@/lib/serverClock';
import { formatTime } from '@/lib/format';
import { cx } from '@/lib/cx';
import { useServerNow } from '@/features/event/useServerNow';
import { spokenDuration } from '@/features/event/Countdown';

const two = (n: number) => String(n).padStart(2, '0');

interface Props {
  /** hold_expires_at from the server. */
  expiresAt: string;
  /** Called once when the displayed time reaches zero. The server decides what actually happens. */
  onElapsed?: () => void;
  className?: string;
}

/**
 * The hold timer. DISPLAY ONLY: it counts against estimated server time, but it
 * never expires anything itself. At zero it says it is checking, and the page asks
 * the server (which is the only authority on whether the hold is gone).
 */
export function HoldTimer({ expiresAt, onElapsed, className }: Props) {
  const now = useServerNow(1000);
  const ms = Date.parse(expiresAt) - now;
  const p = splitDuration(ms);
  const urgent = ms <= 60_000;
  const elapsed = ms <= 0;
  const announce = useAnnounce();
  const saidMinute = useRef(false);
  const saidZero = useRef(false);

  useEffect(() => {
    if (urgent && !elapsed && !saidMinute.current) {
      saidMinute.current = true;
      announce('Less than a minute left to claim your seat.');
    }
    if (elapsed && !saidZero.current) {
      saidZero.current = true;
      onElapsed?.();
    }
  }, [urgent, elapsed, announce, onElapsed]);

  const display = p.hours > 0 ? `${p.hours}:${two(p.minutes)}:${two(p.seconds)}` : `${two(p.minutes)}:${two(p.seconds)}`;

  return (
    <div className={cx('space-y-1', className)}>
      <p className="font-display text-sm font-bold uppercase tracking-widest text-ink-2">Your seat is held for</p>
      {elapsed ? (
        <p className="font-display text-2xl font-bold" role="status">
          Time’s up. Checking with the server…
        </p>
      ) : (
        <>
          <p
            aria-hidden="true"
            data-testid="hold-timer"
            data-urgent={urgent}
            className={cx(
              'tnum inline-block rounded-md border-2 border-ink px-4 py-1 font-display text-5xl font-extrabold leading-tight shadow-pop-sm',
              urgent ? 'bg-tomato-tint text-tomato-deep' : 'bg-sun',
            )}
          >
            {display}
          </p>
          <p className="sr-only">Your seat is held for {spokenDuration(p)}.</p>
          <p className="text-sm text-ink-2">Until {formatTime(expiresAt)}</p>
        </>
      )}
    </div>
  );
}
