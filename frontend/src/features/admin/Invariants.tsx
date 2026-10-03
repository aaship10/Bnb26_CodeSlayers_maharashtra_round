import { useQuery } from '@tanstack/react-query';
import { CircleCheck, OctagonAlert, CircleHelp } from 'lucide-react';
import { cx } from '@/lib/cx';
import { adminApi, adminKeys } from './adminApi';
import { pollInterval } from './logic';
import type { Invariants } from './schemas';

export function useInvariants(eventId: string) {
  return useQuery({
    queryKey: adminKeys.invariants(eventId),
    queryFn: ({ signal }) => adminApi.invariants(eventId, signal),
    refetchInterval: pollInterval(5_000, 3_000),
    retry: false,
  });
}

/** Healthy only when the server says passed AND every counter is zero. Belt and braces. */
export function invariantsHold(inv: Invariants): boolean {
  return inv.passed && inv.oversold === 0 && inv.duplicate_users === 0 && inv.duplicate_seats === 0 && inv.orphaned_holds === 0;
}

export function invariantsSummary(inv: Invariants): string {
  return `oversold ${inv.oversold} · duplicates ${inv.duplicate_users + inv.duplicate_seats} · orphaned holds ${inv.orphaned_holds}`;
}

/**
 * The integrity badge: icon + words + numbers, never colour alone. Green when the
 * allocation is provably clean, loud red when not, neutral while unknown.
 */
export function InvariantsBadge({ query }: { query: ReturnType<typeof useInvariants> }) {
  const inv = query.data;
  const state = !inv ? 'unknown' : invariantsHold(inv) ? 'ok' : 'bad';
  const Icon = state === 'ok' ? CircleCheck : state === 'bad' ? OctagonAlert : CircleHelp;
  const label = state === 'ok' ? 'Invariants hold' : state === 'bad' ? 'Invariant violated' : query.isError ? 'Can’t check invariants' : 'Checking invariants…';

  return (
    <div
      role="status"
      data-testid="invariants"
      data-state={state}
      className={cx(
        'inline-flex flex-wrap items-center gap-x-2 gap-y-0.5 rounded-md border-2 px-3 py-1.5 text-sm',
        state === 'ok' && 'border-ink bg-mint-tint text-ink',
        state === 'bad' && 'border-ink bg-tomato text-white',
        state === 'unknown' && 'border-ink bg-paper-3 text-ink-2',
      )}
    >
      <Icon className="size-4 shrink-0" aria-hidden="true" />
      <span className="font-bold">{label}</span>
      {inv && <span className="tnum font-mono text-xs">{invariantsSummary(inv)}</span>}
    </div>
  );
}
