import { useEffect, useState } from 'react';
import { serverClock } from '@/lib/serverClock';

/**
 * The (estimated) server time in epoch ms, re-rendered every `tickMs`. For
 * DISPLAY only: countdown digits and hold timers. Anything that decides what
 * the user may do comes from the server's phase/state, never from this value.
 */
export function useServerNow(tickMs = 1000): number {
  const [now, setNow] = useState(() => serverClock.now());

  useEffect(() => {
    const tick = () => setNow(serverClock.now());
    tick();
    const id = setInterval(tick, tickMs);
    // Timers are throttled in background tabs; resync the moment the tab is back.
    document.addEventListener('visibilitychange', tick);
    return () => {
      clearInterval(id);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [tickMs]);

  return now;
}
