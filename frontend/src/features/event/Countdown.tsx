import { memo } from 'react';
import { splitDuration, type CountdownParts } from '@/lib/serverClock';
import { useServerNow } from './useServerNow';
import { cx } from '@/lib/cx';

const plural = (n: number, unit: string) => `${n} ${unit}${n === 1 ? '' : 's'}`;

/** "2 days 3 hours", "4 minutes 10 seconds", "less than a minute". Used for assistive tech, at minute resolution. */
export function spokenDuration(p: CountdownParts): string {
  if (p.days > 0) return [plural(p.days, 'day'), p.hours ? plural(p.hours, 'hour') : ''].filter(Boolean).join(' ');
  if (p.hours > 0) return [plural(p.hours, 'hour'), p.minutes ? plural(p.minutes, 'minute') : ''].filter(Boolean).join(' ');
  if (p.minutes > 0) return plural(p.minutes, 'minute');
  return 'less than a minute';
}

const two = (n: number) => String(n).padStart(2, '0');

const Tile = memo(function Tile({ value, label }: { value: string; label: string }) {
  return (
    <div className="flex min-w-[4.25rem] flex-1 flex-col items-center rounded-md border-2 border-ink bg-paper-2 px-2 py-2 shadow-pop-sm sm:min-w-[5.5rem]">
      <span className="tnum font-display text-3xl font-extrabold leading-none sm:text-4xl">{value}</span>
      <span className="mt-1 text-[0.6875rem] font-semibold uppercase tracking-widest text-ink-3">{label}</span>
    </div>
  );
});

interface CountdownProps {
  /** ISO time, from the server. */
  target: string;
  /** "Window opens in" */
  label: string;
  className?: string;
}

/**
 * Ticking countdown to a server timestamp. The digits are aria-hidden (a
 * per-second readout would be unbearable with a screen reader); a sentence that
 * changes only each minute carries the meaning instead.
 *
 * At zero it says "any moment now" and waits for the server to flip the phase.
 * It never opens anything by itself.
 */
export function Countdown({ target, label, className }: CountdownProps) {
  const now = useServerNow(1000);
  const parts = splitDuration(Date.parse(target) - now);
  const reached = parts.totalMs === 0;

  return (
    <div className={cx('space-y-2', className)}>
      <p className="font-display text-sm font-bold uppercase tracking-widest text-ink-2">{label}</p>
      {reached ? (
        <p className="font-display text-2xl font-bold" role="status">
          Any moment now…
        </p>
      ) : (
        <>
          <div className="flex max-w-md gap-2" aria-hidden="true" data-testid="countdown-tiles">
            {parts.days > 0 && <Tile value={String(parts.days)} label={parts.days === 1 ? 'day' : 'days'} />}
            <Tile value={two(parts.hours)} label="hrs" />
            <Tile value={two(parts.minutes)} label="min" />
            <Tile value={two(parts.seconds)} label="sec" />
          </div>
          <p className="sr-only">
            {label} {spokenDuration(parts)}
          </p>
        </>
      )}
    </div>
  );
}
