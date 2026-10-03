import type { ReactNode } from 'react';
import { CircleCheck, OctagonAlert } from 'lucide-react';
import { formatCount } from '@/lib/format';
import { cx } from '@/lib/cx';
import { CiWhisker, StatValue, SyntheticBanner, fmt, isStat, type Unit } from './stats';
import type { SimResults, StatOrNumber } from './schemas';

function Card({ title, children, className }: { title: string; children: ReactNode; className?: string }) {
  return (
    <section className={cx('rounded-lg border-2 border-ink bg-paper-2 p-4 sm:p-5', className)} aria-label={title}>
      <h3 className="mb-4 font-display text-xl">{title}</h3>
      {children}
    </section>
  );
}

function MetricRow({ label, value, unit, whisker, domain }: { label: string; value: StatOrNumber | null; unit: Unit; whisker?: boolean; domain?: [number, number] }) {
  return (
    <div className="grid items-center gap-x-4 gap-y-1 border-t border-rule py-2.5 first:border-t-0 sm:grid-cols-[1fr_14rem]">
      <StatValue value={value} unit={unit} label={label} />
      {whisker && isStat(value) && <CiWhisker stat={value} domain={domain} label={`${label}: ${fmt(value.mean, unit)}, 95% CI ${fmt(value.ci_low, unit)} to ${fmt(value.ci_high, unit)}`} />}
    </div>
  );
}

export function IntegrityBanner({ i }: { i: SimResults['metrics']['integrity'] }) {
  const parts = `oversold ${i.oversold} · duplicate users ${i.duplicate_users} · duplicate seats ${i.duplicate_seats} · orphaned holds ${i.orphaned_holds}${i.draw_verified === null ? '' : ` · draw ${i.draw_verified ? 'verified' : 'NOT verified'}`}`;
  return i.passed ? (
    <div role="status" className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border-2 border-ink bg-mint-tint px-4 py-3" data-testid="integrity" data-passed="true">
      <CircleCheck className="size-5 text-pine" aria-hidden="true" />
      <span className="font-bold">Integrity held</span>
      <span className="tnum font-mono text-xs">{parts}</span>
    </div>
  ) : (
    <div role="alert" className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border-2 border-ink bg-tomato px-4 py-3 text-white" data-testid="integrity" data-passed="false">
      <OctagonAlert className="size-5" aria-hidden="true" />
      <span className="font-bold">Integrity FAILED</span>
      <span className="tnum font-mono text-xs">{parts}</span>
    </div>
  );
}

const presetOf = (d: SimResults['event']['defences']) => (typeof d === 'string' ? d : d.preset);

/** One run: what was simulated, then fairness, system, detection and integrity, every number with its CI and n. */
export function ResultsView({ r, scenarioName }: { r: SimResults; scenarioName?: string }) {
  const f = r.metrics.fairness;
  const s = r.metrics.system;
  const d = r.metrics.detection;
  return (
    <div className="space-y-5">
      <SyntheticBanner target={r.target} synthetic={r.synthetic} />
      <IntegrityBanner i={r.metrics.integrity} />

      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 rounded-md border-2 border-ink bg-paper p-4 text-sm sm:grid-cols-4">
        {[
          ['Scenario', scenarioName ?? r.scenario_id],
          ['Mode', r.event.mode === 'LOTTERY' ? 'Fair draw' : 'First come, first served'],
          ['Defences', presetOf(r.event.defences)],
          ['Seats', formatCount(r.event.inventory)],
          ['People', formatCount(r.population.legit)],
          ['Bot identities', formatCount(r.population.bot_identities)],
          ['Repeats (n)', String(r.repeats)],
          ['Seed', String(r.seed)],
        ].map(([k, v]) => (
          <div key={k}>
            <dt className="text-ink-3">{k}</dt>
            <dd className="font-semibold">{v}</dd>
          </div>
        ))}
      </dl>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Fairness">
          <MetricRow label="Share of seats won by bots" value={f.bot_seat_share} unit="pct" whisker />
          <MetricRow label="Share of entries from bots" value={f.bot_entrant_share} unit="pct" whisker />
          <MetricRow label="A person’s chance of a seat" value={f.human_win_prob} unit="pct" whisker />
          <MetricRow label="People whose entry got through" value={f.human_entry_success_rate} unit="pct" whisker />
          <MetricRow label="Arrival time vs winning (correlation)" value={f.arrival_time_correlation} unit="corr" whisker domain={[-1, 1]} />
          <MetricRow label="Jain fairness index (1 = equal)" value={f.jain_index} unit="ratio" whisker />
          <MetricRow label="Gini (0 = equal)" value={f.gini} unit="ratio" whisker />
        </Card>

        <div className="space-y-5">
          <Card title="Attacker cost per seat won">
            <MetricRow label="Requests" value={f.attacker_cost_per_seat.requests} unit="count" />
            <MetricRow label="Accounts" value={f.attacker_cost_per_seat.accounts} unit="count" />
            <MetricRow label="Proof-of-work hashes" value={f.attacker_cost_per_seat.pow_hashes} unit="count" />
          </Card>
          <Card title="Detection">
            <MetricRow label="Precision" value={d.precision} unit="pct" whisker />
            <MetricRow label="Recall" value={d.recall} unit="pct" whisker />
            <MetricRow label="False positive rate" value={d.false_positive_rate} unit="pct" whisker />
          </Card>
        </div>
      </div>

      <Card title="System">
        <div className="grid gap-5 lg:grid-cols-[1.2fr_1fr]">
          <table className="w-full text-sm">
            <caption className="mb-2 text-left text-sm text-ink-2">Latency percentiles (ms)</caption>
            <thead>
              <tr className="text-left text-ink-3">
                <th className="py-1 font-normal">Endpoint</th>
                <th className="py-1 text-right font-normal">p50</th>
                <th className="py-1 text-right font-normal">p95</th>
                <th className="py-1 text-right font-normal">p99</th>
              </tr>
            </thead>
            <tbody>
              {(['enter', 'status', 'claim'] as const).map((k) => (
                <tr key={k} className="border-t border-rule">
                  <th scope="row" className="py-1.5 text-left font-semibold capitalize">
                    {k}
                  </th>
                  {(['p50', 'p95', 'p99'] as const).map((p) => {
                    const v = s.latency_ms[k][p];
                    return (
                      <td key={p} className="tnum py-1.5 text-right">
                        {isStat(v) ? (
                          <>
                            {fmt(v.mean, 'count')}
                            <span className="block text-xs text-ink-3">
                              {fmt(v.ci_low, 'count')}–{fmt(v.ci_high, 'count')}, n={v.n}
                            </span>
                          </>
                        ) : (
                          fmt(v, 'count')
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          <div>
            <MetricRow label="Throughput" value={s.throughput_rps} unit="rps" />
            <MetricRow label="429s seen by people" value={s.error_rates.http_429_legit} unit="pct" />
            <MetricRow label="429s seen by bots" value={s.error_rates.http_429_bot} unit="pct" />
            <MetricRow label="5xx errors" value={s.error_rates.http_5xx} unit="pct" />
            <MetricRow label="Timeouts" value={s.error_rates.timeout} unit="pct" />
          </div>
        </div>
      </Card>
    </div>
  );
}
