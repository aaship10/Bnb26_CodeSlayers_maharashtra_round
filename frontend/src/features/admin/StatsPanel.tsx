import { useQuery } from '@tanstack/react-query';
import { describeError } from '@/api/errors';
import { formatCount } from '@/lib/format';
import { Alert } from '@/ui/Alert';
import { MockBadge } from '@/ui/Badge';
import { Skeleton } from '@/ui/Skeleton';
import { adminApi, adminKeys } from './adminApi';
import { pollInterval } from './logic';
import type { Stats } from './schemas';

const time = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });

export function useStats(eventId: string) {
  return useQuery({
    queryKey: adminKeys.stats(eventId),
    queryFn: ({ signal }) => adminApi.stats(eventId, signal),
    // Every 3–5 s while healthy, backing off when failing; paused in a hidden tab.
    refetchInterval: pollInterval(3_000, 2_000),
    retry: false,
  });
}

/** 12,900 -> "12.9K" for tiles; exact numbers stay in the table. */
function compact(n: number): string {
  return new Intl.NumberFormat(undefined, { notation: n >= 10_000 ? 'compact' : 'standard', maximumFractionDigits: 1 }).format(n);
}

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="min-w-0 rounded-md border-2 border-ink bg-paper-2 px-4 py-3">
      <p className="text-sm text-ink-2">{label}</p>
      <p className="truncate font-sans text-2xl font-semibold leading-tight text-ink sm:text-3xl">{value}</p>
      {sub && <p className="text-xs text-ink-3">{sub}</p>}
    </div>
  );
}

/**
 * Part-to-whole of the seats: claimed + held, on a track of what's left.
 * Two categorical slots (cobalt, amber: validated pair) with a 2px surface gap;
 * the legend always shows the numbers, so identity is never colour alone.
 */
function AllocationBar({ a }: { a: Stats['allocations'] }) {
  const pct = (n: number) => (a.inventory > 0 ? (n / a.inventory) * 100 : 0);
  const segments = [
    { key: 'claimed', label: 'Claimed', n: a.claimed, color: 'var(--color-cobalt)' },
    { key: 'held', label: 'Held (waiting to claim)', n: a.held, color: 'var(--color-amber)' },
  ].filter((s) => s.n > 0);

  return (
    <figure>
      <figcaption className="mb-2 text-sm font-semibold text-ink-2">Seats ({formatCount(a.inventory)})</figcaption>
      <div
        className="flex h-6 w-full gap-[2px] overflow-hidden rounded-[4px] bg-paper-3"
        role="img"
        aria-label={`${formatCount(a.claimed)} claimed, ${formatCount(a.held)} held, ${formatCount(a.available)} not yet allocated, of ${formatCount(a.inventory)} seats`}
      >
        {segments.map((s) => (
          <div
            key={s.key}
            data-testid={`alloc-${s.key}`}
            title={`${s.label}: ${formatCount(s.n)}`}
            className="h-full transition-[width] duration-500 motion-reduce:transition-none"
            style={{ width: `${pct(s.n)}%`, background: s.color }}
          />
        ))}
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-sm text-ink-2">
        <li className="flex items-center gap-1.5">
          <span className="size-3 rounded-sm" style={{ background: 'var(--color-cobalt)' }} aria-hidden="true" />
          Claimed <b className="tnum text-ink">{formatCount(a.claimed)}</b>
        </li>
        <li className="flex items-center gap-1.5">
          <span className="size-3 rounded-sm" style={{ background: 'var(--color-amber)' }} aria-hidden="true" />
          Held <b className="tnum text-ink">{formatCount(a.held)}</b>
        </li>
        <li className="flex items-center gap-1.5">
          <span className="size-3 rounded-sm border border-rule bg-paper-3" aria-hidden="true" />
          Not allocated <b className="tnum text-ink">{formatCount(a.available)}</b>
        </li>
      </ul>
    </figure>
  );
}

const STATE_ROWS: { key: keyof Stats['by_state']; label: string }[] = [
  { key: 'REGISTERED', label: 'Registered, not entered' },
  { key: 'ENTERED', label: 'Entered, awaiting draw' },
  { key: 'WON', label: 'Won, holding a seat' },
  { key: 'CLAIMED', label: 'Claimed' },
  { key: 'WAITLISTED', label: 'Waitlisted' },
  { key: 'EXPIRED', label: 'Hold expired' },
  { key: 'LOST', label: 'Not picked' },
];

/** Seven classes that all matter: a table, with one single-hue magnitude bar per row. */
function StateTable({ by }: { by: Stats['by_state'] }) {
  const max = Math.max(1, ...Object.values(by));
  return (
    <table className="w-full text-sm">
      <caption className="mb-2 text-left text-sm font-semibold text-ink-2">People by state</caption>
      <thead className="sr-only">
        <tr>
          <th>State</th>
          <th>People</th>
          <th>Share</th>
        </tr>
      </thead>
      <tbody>
        {STATE_ROWS.map((r) => (
          <tr key={r.key} className="border-t border-rule">
            <th scope="row" className="py-1.5 pr-3 text-left font-normal text-ink-2">
              {r.label}
            </th>
            <td className="tnum py-1.5 pr-3 text-right font-semibold text-ink">{formatCount(by[r.key])}</td>
            <td className="w-1/2 py-1.5" aria-hidden="true">
              <div className="h-2.5 rounded-r-[4px] bg-cobalt" style={{ width: `${(by[r.key] / max) * 100}%`, minWidth: by[r.key] > 0 ? 2 : 0 }} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function StatsPanel({ query }: { query: ReturnType<typeof useStats> }) {
  const s = query.data;
  return (
    <section aria-labelledby="stats-h" className="space-y-4 rounded-lg border-2 border-ink bg-paper p-4 sm:p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="stats-h" className="font-display text-xl">
          Live numbers
        </h2>
        <div className="flex items-center gap-3">
          {s?.synthetic && <MockBadge inline />}
          {query.dataUpdatedAt > 0 && <span className="tnum text-xs text-ink-3">Updated {time.format(query.dataUpdatedAt)}</span>}
        </div>
      </div>

      {query.isError && (
        <Alert tone="offline" title={s ? 'Numbers paused' : 'Can’t load the numbers'}>
          {describeError(query.error).body} Retrying with backoff.
        </Alert>
      )}

      {!s && !query.isError && <Skeleton className="h-64" />}

      {s && (
        <>
          <div className="grid grid-cols-2 gap-3 2xl:grid-cols-4">
            <Tile label="Entrants" value={compact(s.entrants)} sub={s.entrants >= 10_000 ? formatCount(s.entrants) : undefined} />
            <Tile label="Seats claimed" value={formatCount(s.allocations.claimed)} sub={`of ${formatCount(s.allocations.inventory)}`} />
            <Tile label="Holds active" value={formatCount(s.holds.active)} sub={`${formatCount(s.holds.expired)} expired`} />
            <Tile label="Waitlisted" value={compact(s.by_state.WAITLISTED)} />
          </div>
          <AllocationBar a={s.allocations} />
          <StateTable by={s.by_state} />
        </>
      )}
    </section>
  );
}
