import type { ReactNode } from 'react';
import { cx } from '@/lib/cx';
import type { Phase } from '@/api/schemas';

export type BadgeTone = 'neutral' | 'tomato' | 'sun' | 'mint' | 'cobalt';

const tones: Record<BadgeTone, string> = {
  neutral: 'bg-paper-3 text-ink-2',
  tomato: 'bg-tomato-tint text-tomato-deep',
  sun: 'bg-sun-tint text-ink',
  mint: 'bg-mint-tint text-pine',
  cobalt: 'bg-cobalt-tint text-cobalt',
};

const dots: Record<BadgeTone, string> = {
  neutral: 'bg-ink-3',
  tomato: 'bg-tomato',
  sun: 'bg-sun',
  mint: 'bg-pine',
  cobalt: 'bg-cobalt',
};

export function Badge({ tone = 'neutral', dot, children, className }: { tone?: BadgeTone; dot?: boolean; children: ReactNode; className?: string }) {
  return (
    <span
      className={cx(
        'inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border-2 border-ink px-2.5 py-0.5 font-display text-xs font-bold uppercase tracking-wider',
        tones[tone],
        className,
      )}
    >
      {dot && <span className={cx('size-2 rounded-full border border-ink', dots[tone])} aria-hidden="true" />}
      {children}
    </span>
  );
}

// Phase wording is for attendees, not engineers. The label always carries the meaning; colour only reinforces it.
const phases: Record<Phase, { label: string; tone: BadgeTone; dot?: boolean }> = {
  DRAFT: { label: 'Not announced', tone: 'neutral' },
  SCHEDULED: { label: 'Opens soon', tone: 'cobalt' },
  OPEN: { label: 'Window open', tone: 'mint', dot: true },
  DRAWING: { label: 'Drawing', tone: 'sun', dot: true },
  CLAIMING: { label: 'Claiming', tone: 'sun', dot: true },
  CLOSED: { label: 'Closed', tone: 'neutral' },
};

export function phaseLabel(phase: Phase): string {
  return phases[phase].label;
}

export function PhaseBadge({ phase, className }: { phase: Phase; className?: string }) {
  const p = phases[phase];
  return (
    <Badge tone={p.tone} dot={p.dot} className={className}>
      {p.label}
    </Badge>
  );
}

/**
 * Anything backed by the mock server or flagged synthetic by the simulator must
 * wear this. It is deliberately loud: a stamp, not a footnote.
 */
export function MockBadge({ className, inline }: { className?: string; inline?: boolean }) {
  return (
    <span
      role="note"
      className={cx(
        'inline-block rounded-sm border-2 border-dashed border-tomato-deep bg-paper-2/70 px-2 py-0.5 font-mono text-xs font-bold uppercase tracking-widest text-tomato-deep',
        !inline && 'animate-stamp',
        className,
      )}
      style={inline ? undefined : ({ ['--stamp-tilt' as string]: '-3deg' } as React.CSSProperties)}
    >
      Mock / synthetic data
    </span>
  );
}
