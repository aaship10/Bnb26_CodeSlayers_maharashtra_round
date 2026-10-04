import { useState } from 'react';
import { Area, Bar, BarChart, CartesianGrid, ComposedChart, ErrorBar, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { ChartData } from './schemas';
import { VIZ } from './stats';

type Row = Record<string, number | string | [number, number]>;

/** Percent formatting when every value is a share (0..1) and the axis isn't a time. */
export function isShareChart(c: ChartData): boolean {
  if (/ms|latency/i.test(c.y_label)) return false;
  return c.series.every((s) => s.points.every((p) => p.y >= 0 && p.y <= 1 && (p.ci_high ?? 0) <= 1));
}

export function isNumericX(c: ChartData): boolean {
  return c.series.every((s) => s.points.every((p) => typeof p.x === 'number'));
}

/** Log x when the data spans two or more orders of magnitude (request multipliers, identity counts). */
export function wantsLogX(c: ChartData): boolean {
  if (!isNumericX(c)) return false;
  const xs = c.series.flatMap((s) => s.points.map((p) => p.x as number));
  const min = Math.min(...xs);
  return min > 0 && Math.max(...xs) / min >= 100;
}

/** Pivot series into rows keyed by x: { x, [name]: y, [name__band]: [lo, hi], [name__err]: [y-lo, hi-y] }. */
export function toRows(c: ChartData): Row[] {
  const byX = new Map<string, Row>();
  for (const s of c.series) {
    for (const p of s.points) {
      const k = String(p.x);
      const row = byX.get(k) ?? { x: p.x };
      row[s.name] = p.y;
      if (p.ci_low !== undefined && p.ci_high !== undefined) {
        row[`${s.name}__band`] = [p.ci_low, p.ci_high];
        row[`${s.name}__err`] = [p.y - p.ci_low, p.ci_high - p.y];
      }
      byX.set(k, row);
    }
  }
  return [...byX.values()];
}

const pct = (v: number) => `${Math.round(v * 1000) / 10}%`;
const num = (v: number) => new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(v);

function TooltipBody({ active, payload, label, c, share }: { active?: boolean; payload?: { dataKey?: unknown; color?: string; payload: Row }[]; label?: string | number; c: ChartData; share: boolean }) {
  if (!active || !payload?.length) return null;
  const row = payload[0]!.payload;
  const f = share ? pct : num;
  return (
    <div className="rounded-md border-2 border-ink bg-paper-2 px-3 py-2 text-sm shadow-pop-sm">
      <p className="mb-1 text-xs text-ink-3">
        {c.x_label}: {typeof label === 'number' ? num(label) : label}
      </p>
      {c.series.map((s, i) => {
        const y = row[s.name];
        if (typeof y !== 'number') return null;
        const band = row[`${s.name}__band`] as [number, number] | undefined;
        return (
          <p key={s.name} className="flex items-center gap-2">
            <span className="inline-block h-0.5 w-3" style={{ background: VIZ[i % VIZ.length] }} aria-hidden="true" />
            <b className="tnum text-ink">{f(y)}</b>
            {band && <span className="tnum text-xs text-ink-3">({f(band[0])}–{f(band[1])})</span>}
            <span className="text-ink-2">{s.name}</span>
          </p>
        );
      })}
    </div>
  );
}

/** Accessible equivalent of every chart: the same numbers, CIs included. */
export function ChartTable({ c }: { c: ChartData }) {
  const share = isShareChart(c);
  const f = share ? pct : num;
  const rows = toRows(c);
  return (
    <table className="w-full text-sm">
      <caption className="sr-only">{c.title}</caption>
      <thead>
        <tr className="text-left text-ink-3">
          <th className="py-1 font-normal">{c.x_label}</th>
          {c.series.map((s) => (
            <th key={s.name} className="py-1 font-normal">
              {s.name}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={String(r.x)} className="border-t border-rule">
            <th scope="row" className="tnum py-1 text-left font-normal">
              {typeof r.x === 'number' ? num(r.x) : String(r.x)}
            </th>
            {c.series.map((s) => {
              const y = r[s.name];
              const band = r[`${s.name}__band`] as [number, number] | undefined;
              return (
                <td key={s.name} className="tnum py-1">
                  {typeof y === 'number' ? f(y) : '–'}
                  {band && <span className="block text-xs text-ink-3">95% CI {f(band[0])}–{f(band[1])}</span>}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * One chart dataset. Numeric x: lines (2px) with a 95% CI band at low opacity.
 * Categorical x: bars (max 24px, rounded data-end) with CI error bars.
 * Single y axis, legend for 2+ series, hover tooltip, and a table view.
 */
export function ChartView({ c }: { c: ChartData }) {
  const [table, setTable] = useState(false);
  const share = isShareChart(c);
  const numeric = isNumericX(c);
  const log = wantsLogX(c);
  const rows = toRows(c);
  const yFmt = share ? pct : num;
  const ticks = numeric ? [...new Set(rows.map((r) => r.x as number))] : undefined;
  const common = {
    grid: <CartesianGrid vertical={false} stroke="var(--color-rule)" strokeWidth={1} />,
    y: <YAxis tickFormatter={yFmt} tick={{ fill: 'var(--color-ink-2)', fontSize: 12 }} axisLine={false} tickLine={false} width={56} domain={share ? [0, 'auto'] : ['auto', 'auto']} />,
    tooltip: <Tooltip content={<TooltipBody c={c} share={share} />} cursor={numeric ? { stroke: 'var(--color-ink-3)', strokeWidth: 1 } : { fill: 'var(--color-paper-3)' }} />,
    // Legends follow the series order (= palette slot order), not Recharts' default alphabetical sort.
    legend:
      c.series.length > 1 ? (
        <Legend wrapperStyle={{ fontSize: 13, color: 'var(--color-ink-2)' }} itemSorter={(item) => c.series.findIndex((s) => s.name === item.value)} />
      ) : null,
  };

  return (
    <figure className="space-y-2">
      <figcaption>
        <p className="font-display text-lg font-bold">{c.title}</p>
        <p className="text-xs text-ink-3">
          y: {c.y_label} · x: {c.x_label}
        </p>
      </figcaption>
      {table ? (
        <ChartTable c={c} />
      ) : (
        <div className="h-72 w-full" role="img" aria-label={`${c.title}. ${c.series.length} series. Use “Show as table” for the values.`}>
          <ResponsiveContainer width="100%" height="100%">
            {numeric ? (
              <ComposedChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
                {common.grid}
                <XAxis
                  dataKey="x"
                  type="number"
                  scale={log ? 'log' : 'linear'}
                  domain={['dataMin', 'dataMax']}
                  ticks={ticks}
                  tickFormatter={num}
                  tick={{ fill: 'var(--color-ink-2)', fontSize: 12 }}
                  stroke="var(--color-rule)"
                />
                {common.y}
                {common.tooltip}
                {common.legend}
                {c.series.map((s, i) => (
                  <Area key={`${s.name}-band`} dataKey={`${s.name}__band`} stroke="none" fill={VIZ[i % VIZ.length]} fillOpacity={0.12} isAnimationActive={false} legendType="none" tooltipType="none" activeDot={false} />
                ))}
                {c.series.map((s, i) => (
                  <Line
                    key={s.name}
                    dataKey={s.name}
                    stroke={VIZ[i % VIZ.length]}
                    strokeWidth={2}
                    dot={{ r: 4, fill: VIZ[i % VIZ.length], stroke: 'var(--color-paper-2)', strokeWidth: 2 }}
                    activeDot={{ r: 6, stroke: 'var(--color-paper-2)', strokeWidth: 2 }}
                    isAnimationActive={false}
                    legendType="line"
                  />
                ))}
              </ComposedChart>
            ) : (
              <BarChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }} barGap={2}>
                {common.grid}
                <XAxis dataKey="x" tick={{ fill: 'var(--color-ink-2)', fontSize: 12 }} stroke="var(--color-rule)" />
                {common.y}
                {common.tooltip}
                {common.legend}
                {c.series.map((s, i) => (
                  <Bar key={s.name} dataKey={s.name} fill={VIZ[i % VIZ.length]} maxBarSize={24} radius={[4, 4, 0, 0]} isAnimationActive={false}>
                    <ErrorBar dataKey={`${s.name}__err`} width={6} stroke="var(--color-ink)" strokeWidth={1.5} />
                  </Bar>
                ))}
              </BarChart>
            )}
          </ResponsiveContainer>
        </div>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2">
        {c.notes ? <p className="text-xs text-ink-3">{c.notes}</p> : <span />}
        <button type="button" onClick={() => setTable((t) => !t)} className="link text-sm font-semibold" aria-pressed={table}>
          {table ? 'Show as chart' : 'Show as table'}
        </button>
      </div>
    </figure>
  );
}
