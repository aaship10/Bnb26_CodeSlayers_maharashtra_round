import type { Connection } from './LiveStatus';
import { cx } from '@/lib/cx';

const LABELS: Record<Connection, { text: string; dot: string; pulse?: boolean }> = {
  connecting: { text: 'Connecting…', dot: 'bg-ink-3' },
  live: { text: 'Live', dot: 'bg-pine', pulse: true },
  reconnecting: { text: 'Reconnecting…', dot: 'bg-sun' },
  polling: { text: 'Checking every few seconds', dot: 'bg-cobalt' },
  paused: { text: 'Paused while this tab is hidden', dot: 'bg-ink-3' },
  offline: { text: 'You’re offline', dot: 'bg-tomato' },
  final: { text: 'Final result', dot: 'bg-ink' },
  stopped: { text: 'Not updating', dot: 'bg-ink-3' },
};

/** Small, honest connection badge: says how fresh what you're looking at is. */
export function LiveIndicator({ connection, className }: { connection: Connection; className?: string }) {
  const l = LABELS[connection];
  return (
    <span
      className={cx('inline-flex items-center gap-2 rounded-full border-2 border-ink bg-paper-2 px-3 py-1 text-xs font-semibold', className)}
      data-testid="live-indicator"
      data-connection={connection}
    >
      <span className="relative flex size-2.5" aria-hidden="true">
        {l.pulse && <span className={cx('absolute inline-flex size-full animate-ping rounded-full opacity-60 motion-reduce:hidden', l.dot)} />}
        <span className={cx('relative inline-flex size-2.5 rounded-full border border-ink', l.dot)} />
      </span>
      {l.text}
    </span>
  );
}
