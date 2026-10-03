import { useEffect, useRef } from 'react';
import { serverClock } from '@/lib/serverClock';
import { withJitter } from '@/lib/backoff';

// setTimeout silently misbehaves above 2^31-1 ms (~24.8 days)
const MAX_TIMEOUT_MS = 2 ** 31 - 1;

/** Random lag after a deadline before we ask the server what changed. Spreads the herd over about 4 s. */
export const DEADLINE_JITTER = { baseMs: 300, spreadMs: 3700 } as const;

/**
 * When a server deadline (window opens, window closes) passes, ask the server
 * for the new phase, but NOT at the instant it passes: every client would hit
 * the API in the same second. Each client waits a random 0.3 to 4 s first.
 * (Live pushes over SSE, Stage 3, make this a fallback rather than the main path.)
 *
 * A deadline already in the past at mount fires one jittered refetch at most,
 * then stops: the server's phase is the truth, and a skewed clock must not
 * turn into a polling loop.
 */
export function useDeadlineRefetch(deadlineIso: string | undefined, refetch: () => unknown): void {
  const refetchRef = useRef(refetch);
  refetchRef.current = refetch;

  useEffect(() => {
    if (!deadlineIso) return;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const schedule = () => {
      const ms = serverClock.msUntil(deadlineIso);
      if (ms > MAX_TIMEOUT_MS) {
        timer = setTimeout(schedule, MAX_TIMEOUT_MS);
        return;
      }
      const delay = Math.max(0, ms) + withJitter(DEADLINE_JITTER.baseMs, DEADLINE_JITTER.spreadMs);
      timer = setTimeout(() => void refetchRef.current(), delay);
    };

    schedule();
    return () => clearTimeout(timer);
  }, [deadlineIso]);
}
