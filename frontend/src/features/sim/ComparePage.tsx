import { useMemo } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeft } from 'lucide-react';
import { describeError } from '@/api/errors';
import { formatCount } from '@/lib/format';
import { Alert } from '@/ui/Alert';
import { Skeleton } from '@/ui/Skeleton';
import { AdminShell } from '@/features/admin/AdminShell';
import { IntegrityBanner } from './ResultsView';
import { simApi, simKeys } from './simApi';
import { SyntheticBanner, VIZ, ciText, fmt, isStat, type Unit } from './stats';
import type { SimResults, Stat, StatOrNumber } from './schemas';

/** Fixed order and colours: Fair Drop is slot 1, FCFS slot 2, everywhere on this page. */
const SIDES = [
  { key: 'lottery', label: 'Fair Drop', color: VIZ[0]! },
  { key: 'fcfs', label: 'First come, first served', color: VIZ[1]! },
] as const;

/** Differences between two runs that make a side-by-side comparison misleading. */
export function attackMismatch(a: SimResults, b: SimResults): string[] {
  const out: string[] = [];
  if (a.scenario_id !== b.scenario_id) out.push(`different scenarios (${a.scenario_id} vs ${b.scenario_id})`);
  if (a.population.legit !== b.population.legit) out.push('different numbers of people');
  if (a.population.bot_identities !== b.population.bot_identities) out.push('different numbers of bot identities');
  if (a.event.inventory !== b.event.inventory) out.push('different seat counts');
  if (a.event.mode === b.event.mode) out.push(`both runs use ${a.event.mode}`);
  return out;
}

const statOf = (v: StatOrNumber | null): Stat | null => (v === null ? null : isStat(v) ? v : { mean: v, ci_low: v, ci_high: v, n: 1 });

/** Two intervals on ONE shared axis, so the gap between them is honest. */
function PairedWhisker({ a, b, domain, unit, label }: { a: Stat; b: Stat; domain: [number, number]; unit: Unit; label: string }) {
  const W = 320;
  const x = (v: number) => ((Math.min(domain[1], Math.max(domain[0], v)) - domain[0]) / (domain[1] - domain[0])) * W;
  const rows = [
    { s: a, ...SIDES[0] },
    { s: b, ...SIDES[1] },
  ];
  return (
    <svg
      viewBox={`-8 0 ${W + 16} 44`}
      className="h-11 w-full"
      role="img"
      aria-label={`${label}: Fair Drop ${fmt(a.mean, unit)} (${ciText(a, unit)}); FCFS ${fmt(b.mean, unit)} (${ciText(b, unit)})`}
    >
      <line x1={0} x2={W} y1={40} y2={40} stroke="var(--color-rule)" strokeWidth={1} />
      {[0, 0.5, 1].map((t) => (
        <line key={t} x1={t * W} x2={t * W} y1={36} y2={42} stroke="var(--color-rule)" strokeWidth={1} />
      ))}
      {rows.map((r, i) => (
        <g key={r.key}>
          <line x1={x(r.s.ci_low)} x2={x(r.s.ci_high)} y1={10 + i * 16} y2={10 + i * 16} stroke={r.color} strokeWidth={3} strokeLinecap="round" />
          <circle cx={x(r.s.mean)} cy={10 + i * 16} r={5} fill={r.color} stroke="var(--color-paper-2)" strokeWidth={2} />
        </g>
      ))}
    </svg>
  );
}

interface Row {
  label: string;
  pick: (r: SimResults) => StatOrNumber | null;
  unit: Unit;
  domain?: [number, number];
  better: 'lower' | 'higher';
}

const ROWS: Row[] = [
  { label: 'Share of seats won by bots', pick: (r) => r.metrics.fairness.bot_seat_share, unit: 'pct', domain: [0, 1], better: 'lower' },
  { label: 'A person’s chance of a seat', pick: (r) => r.metrics.fairness.human_win_prob, unit: 'pct', domain: [0, 0.02], better: 'higher' },
  { label: 'People whose entry got through', pick: (r) => r.metrics.fairness.human_entry_success_rate, unit: 'pct', domain: [0, 1], better: 'higher' },
  { label: 'Arrival time vs winning', pick: (r) => r.metrics.fairness.arrival_time_correlation, unit: 'corr', domain: [-1, 1], better: 'lower' },
  { label: 'Jain fairness index', pick: (r) => r.metrics.fairness.jain_index, unit: 'ratio', domain: [0, 1], better: 'higher' },
  { label: 'Entry latency p95', pick: (r) => r.metrics.system.latency_ms.enter.p95, unit: 'ms', better: 'lower' },
  { label: '429s seen by people', pick: (r) => r.metrics.system.error_rates.http_429_legit, unit: 'pct', domain: [0, 0.05], better: 'lower' },
];

function delta(a: Stat, b: Stat, unit: Unit): string {
  const d = a.mean - b.mean;
  const sign = d > 0 ? '+' : d < 0 ? '−' : '±';
  if (unit === 'pct') return `${sign}${fmt(Math.abs(d), 'pct').replace('%', '')} pp`;
  return `${sign}${fmt(Math.abs(d), unit)}`;
}

function RunPicker({ label, value, onChange, runs, color }: { label: string; value: string; onChange: (v: string) => void; runs: { run_id: string; scenario_name?: string; scenario_id: string; params?: Record<string, unknown> }[]; color: string }) {
  const id = `pick-${label}`;
  return (
    <div>
      <label htmlFor={id} className="mb-1 flex items-center gap-2 font-display text-sm font-bold">
        <span className="size-3 rounded-full" style={{ background: color }} aria-hidden="true" />
        {label}
      </label>
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)} className="min-h-11 w-full rounded-md border-2 border-ink bg-paper-2 px-3">
        <option value="">Choose a finished run…</option>
        {runs.map((r) => (
          <option key={r.run_id} value={r.run_id}>
            {r.run_id} · {r.scenario_name ?? r.scenario_id} · {String(r.params?.mode ?? '')} · {String(r.params?.defence_preset ?? '')}
          </option>
        ))}
      </select>
    </div>
  );
}

export function Component() {
  const [params, setParams] = useSearchParams();
  const runs = useQuery({ queryKey: simKeys.runs, queryFn: ({ signal }) => simApi.runs(signal) });
  const done = useMemo(() => (runs.data ?? []).filter((r) => r.status === 'done'), [runs.data]);
  const byMode = (m: string) => done.filter((r) => r.params?.mode === m);

  // Defaults: the latest finished run of each mode, unless the URL says otherwise. Run ids aren't personal data.
  const lotteryId = params.get('lottery') ?? (params.get('b') && done.find((r) => r.run_id === params.get('b'))?.params?.mode === 'LOTTERY' ? params.get('b')! : byMode('LOTTERY')[0]?.run_id ?? '');
  const fcfsId = params.get('fcfs') ?? (params.get('b') && done.find((r) => r.run_id === params.get('b'))?.params?.mode === 'FCFS' ? params.get('b')! : byMode('FCFS')[0]?.run_id ?? '');

  const a = useQuery({ queryKey: simKeys.results(lotteryId), queryFn: ({ signal }) => simApi.results(lotteryId, signal), enabled: !!lotteryId, staleTime: Infinity });
  const b = useQuery({ queryKey: simKeys.results(fcfsId), queryFn: ({ signal }) => simApi.results(fcfsId, signal), enabled: !!fcfsId, staleTime: Infinity });

  const set = (k: 'lottery' | 'fcfs', v: string) => {
    const next = new URLSearchParams(params);
    next.delete('b');
    next.set('lottery', k === 'lottery' ? v : lotteryId);
    next.set('fcfs', k === 'fcfs' ? v : fcfsId);
    setParams(next, { replace: true });
  };

  const ra = a.data;
  const rb = b.data;
  const mismatch = ra && rb ? attackMismatch(ra, rb) : [];

  return (
    <AdminShell title="Compare">
      <Link to="/admin/sim" className="inline-flex items-center gap-1 text-sm font-semibold text-ink-2 hover:text-ink">
        <ArrowLeft className="size-4" aria-hidden="true" /> Simulator
      </Link>
      <div>
        <h1 className="font-display text-3xl">Same attack, two ways to allocate</h1>
        <p className="text-ink-2">Pick a Fair Drop run and a first-come-first-served run that faced the same crowd and the same bots.</p>
      </div>

      {runs.error && (
        <Alert tone={describeError(runs.error).tone} title="Can’t list runs">
          {describeError(runs.error).body}
        </Alert>
      )}
      <div className="grid gap-4 sm:grid-cols-2">
        <RunPicker label="Fair Drop run" value={lotteryId} onChange={(v) => set('lottery', v)} runs={done} color={SIDES[0].color} />
        <RunPicker label="FCFS run" value={fcfsId} onChange={(v) => set('fcfs', v)} runs={done} color={SIDES[1].color} />
      </div>

      {(a.isFetching || b.isFetching) && !(ra && rb) && <Skeleton className="h-80" />}
      {!lotteryId || !fcfsId ? <Alert title="Two finished runs needed">Start one run in each mode from the simulator (demo presets 1 and 2 do exactly this).</Alert> : null}

      {ra && rb && (
        <div className="space-y-5">
          <SyntheticBanner target={ra.target === 'mock' || rb.target === 'mock' ? 'mock' : 'real'} synthetic={ra.synthetic || rb.synthetic} />
          {mismatch.length > 0 && (
            <Alert tone="warn" title="These runs didn’t face the same conditions">
              {mismatch.join('; ')}. Treat the comparison with care.
            </Alert>
          )}

          <section aria-labelledby="cmp-h" className="rounded-lg border-2 border-ink bg-paper-2 p-4 sm:p-5">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
              <h2 id="cmp-h" className="font-display text-xl">
                Side by side
              </h2>
              <ul className="flex flex-wrap gap-4 text-sm text-ink-2" aria-label="Legend">
                {SIDES.map((s) => (
                  <li key={s.key} className="flex items-center gap-1.5">
                    <span className="size-3 rounded-full" style={{ background: s.color }} aria-hidden="true" />
                    {s.label}
                  </li>
                ))}
                <li className="text-ink-3">dot = mean, bar = 95% CI</li>
              </ul>
            </div>
            <p className="mb-3 text-sm text-ink-3">
              {formatCount(ra.population.legit)} people, {formatCount(ra.population.bot_identities)} bot identities, {formatCount(ra.event.inventory)} seats. n = {ra.repeats} and {rb.repeats} repeats.
            </p>

            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-ink-3">
                  <th className="py-1 font-normal">Metric</th>
                  <th className="py-1 font-normal">Fair Drop</th>
                  <th className="py-1 font-normal">FCFS</th>
                  <th className="hidden py-1 font-normal md:table-cell">On one axis</th>
                  <th className="py-1 text-right font-normal">Difference</th>
                </tr>
              </thead>
              <tbody>
                {ROWS.map((row) => {
                  const sa = statOf(row.pick(ra));
                  const sb = statOf(row.pick(rb));
                  if (!sa || !sb) return null;
                  const domain = row.domain ?? [0, Math.max(sa.ci_high, sb.ci_high) * 1.1 || 1];
                  const fairWins = row.better === 'lower' ? sa.mean < sb.mean : sa.mean > sb.mean;
                  return (
                    <tr key={row.label} className="border-t border-rule align-top" data-testid={`cmp-${row.label}`}>
                      <th scope="row" className="py-2.5 pr-3 text-left font-semibold">
                        {row.label}
                        <span className="block text-xs font-normal text-ink-3">{row.better} is better</span>
                      </th>
                      {[sa, sb].map((s, i) => (
                        <td key={i} className="py-2.5 pr-3">
                          <span className="tnum block text-base font-semibold">{fmt(s.mean, row.unit)}</span>
                          {s.n > 1 ? (
                            <>
                              <span className="tnum block text-xs text-ink-3">
                                95% CI {fmt(s.ci_low, row.unit)}–{fmt(s.ci_high, row.unit)}
                              </span>
                              <span className="tnum block text-xs text-ink-3">n = {s.n}</span>
                            </>
                          ) : (
                            <span className="block text-xs text-ink-3">single value</span>
                          )}
                        </td>
                      ))}
                      <td className="hidden w-[22rem] py-2.5 md:table-cell">
                        <PairedWhisker a={sa} b={sb} domain={domain} unit={row.unit} label={row.label} />
                      </td>
                      <td className="tnum py-2.5 text-right">
                        <span className="font-semibold">{delta(sa, sb, row.unit)}</span>
                        <span className="block text-xs text-ink-3">{fairWins ? 'Fair Drop better' : 'FCFS better'}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="mt-3 text-xs text-ink-3">Difference = Fair Drop mean minus FCFS mean. Judge significance from the intervals, not from the difference alone.</p>
          </section>

          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <p className="mb-1 text-sm font-semibold">Fair Drop integrity</p>
              <IntegrityBanner i={ra.metrics.integrity} />
            </div>
            <div>
              <p className="mb-1 text-sm font-semibold">FCFS integrity</p>
              <IntegrityBanner i={rb.metrics.integrity} />
            </div>
          </div>
        </div>
      )}
    </AdminShell>
  );
}
