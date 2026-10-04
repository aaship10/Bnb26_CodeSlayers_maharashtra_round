import { useEffect, useState } from 'react';

/**
 * Whole seconds left of a server-given DURATION (Retry-After, next retry), counted
 * down on the monotonic clock. No wall-clock comparison, so a wrong device clock
 * can't affect it. Restarts whenever `resetKey` changes.
 */
export function useCountdownSeconds(durationMs: number | undefined, resetKey: unknown): number {
  const [left, setLeft] = useState(() => (durationMs ? Math.ceil(durationMs / 1000) : 0));
  useEffect(() => {
    if (!durationMs || durationMs <= 0) {
      setLeft(0);
      return;
    }
    const endsAt = performance.now() + durationMs;
    const tick = () => setLeft(Math.max(0, Math.ceil((endsAt - performance.now()) / 1000)));
    tick();
    const id = setInterval(tick, 250);
    return () => clearInterval(id);
  }, [durationMs, resetKey]);
  return left;
}
