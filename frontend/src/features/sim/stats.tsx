import type { ReactNode } from 'react';
import { cx } from '@/lib/cx';
import type { Stat, StatOrNumber } from './schemas';

export type Unit = 'pct' | 'ratio' | 'ms' | 'rps' | 'count' | 'corr';

export const VIZ = ['var(--color-viz-1)', 'var(--color-viz-2)', 'var(--color-viz-3)', 'var(--color-viz-4)', 'var(--color-viz-5)'];

const nf = (digits: number) => new Intl.NumberFormat(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
const compact = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });

export function fmt(v: number, unit: Unit): string {
  switch (unit) {
    case 'pct': {
      const p = v * 100;
      return `${nf(Math.abs(p) < 1 && p !== 0 ? 2 : 1).format(p)}%`;
    }
    case 'ratio':
    case 'corr':
      return nf(3).format(v);
    case 'ms':
      return `${nf(0).format(v)} ms`;
    case 'rps':
      return `${nf(0).format(v)}/s`;
    case 'count':
      return v >= 100_000 ? compact.format(v) : nf(0).format(v);
  }
}

export const isStat = (v: StatOrNumber | null | undefined): v is Stat => typeof v === 'object' && v !== null && 'mean' in v;

/** "95% CI 10.1%–14.6% · n = 10". The CI and n are never optional on screen. */
export function ciText(s: Stat, unit: Unit): string {
  return `95% CI ${fmt(s.ci_low, unit)}–${fmt(s.ci_high, unit)} · n = ${s.n}`;
}

/** A mean with its CI and n, or an explicitly labelled single value. Never a bare mean. */
export function StatValue({ value, unit, label, emphasis, hint }: { value: StatOrNumber | null; unit: Unit; label: string; emphasis?: boolean; hint?: ReactNode }) {
  return (
    <div className="min-w-0">
      <p className="text-sm text-ink-2">{label}</p>
      {value === null ? (
        <p className="text-sm italic text-ink-3">Not applicable</p>
      ) : isStat(value) ? (
        <>
          <p className={cx('font-semibold leading-tight text-ink', emphasis ? 'text-3xl' : 'text-xl')}>{fmt(value.mean, unit)}</p>
          <p className="tnum text-xs text-ink-3">{ciText(value, unit)}</p>
        </>
      ) : (
        <>
          <p className={cx('font-semibold leading-tight text-ink', emphasis ? 'text-3xl' : 'text-xl')}>{fmt(value, unit)}</p>
          <p className="text-xs text-ink-3">single value, no CI reported</p>
        </>
      )}
      {hint && <p className="mt-0.5 text-xs text-ink-3">{hint}</p>}
    </div>
  );
}

/**
 * The interval as a mark: a dot at the mean and a line across the CI, on a fixed
 * 0–100% track (or −1..1 for correlations) so values are comparable at a glance.
 */
export function CiWhisker({ stat, color = VIZ[0]!, domain = [0, 1], label }: { stat: Stat; color?: string; domain?: [number, number]; label: string }) {
  const W = 200;
  const x = (v: number) => ((Math.min(domain[1], Math.max(domain[0], v)) - domain[0]) / (domain[1] - domain[0])) * W;
  return (
    <svg viewBox={`-6 0 ${W + 12} 16`} className="h-4 w-full max-w-56" role="img" aria-label={label}>
      <line x1={0} x2={W} y1={8} y2={8} stroke="var(--color-rule)" strokeWidth={1} />
      <line x1={x(stat.ci_low)} x2={x(stat.ci_high)} y1={8} y2={8} stroke={color} strokeWidth={3} strokeLinecap="round" />
      <circle cx={x(stat.mean)} cy={8} r={4.5} fill={color} stroke="var(--color-paper-2)" strokeWidth={2} />
    </svg>
  );
}

/** Loud, unmissable label for anything simulated or produced by the mock. */
export function SyntheticBanner({ target, synthetic }: { target?: 'mock' | 'real'; synthetic?: boolean }) {
  if (target !== 'mock' && !synthetic) return null;
  return (
    <div role="note" className="flex flex-wrap items-center gap-3 rounded-md border-2 border-dashed border-tomato-deep bg-tomato-tint/50 px-4 py-2" data-testid="synthetic-banner">
      <span className="font-mono text-sm font-bold uppercase tracking-widest text-tomato-deep">Mock / synthetic data</span>
      <span className="text-sm text-ink-2">
        {target === 'mock' ? 'Produced by the mock simulator, not by load against the real stack.' : 'Marked synthetic by the simulator.'} Don’t quote these numbers as measurements.
      </span>
    </div>
  );
}
