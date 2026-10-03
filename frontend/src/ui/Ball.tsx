import { cx } from '@/lib/cx';

export type BallColor = 'tomato' | 'cobalt' | 'sun' | 'mint' | 'paper';

// numeral colour is picked per ball so it is always readable (see tokens.test.ts)
const fills: Record<BallColor, { fill: string; num: string }> = {
  tomato: { fill: 'var(--color-tomato)', num: 'var(--color-ink)' },
  cobalt: { fill: 'var(--color-cobalt)', num: 'var(--color-ink)' },
  sun: { fill: 'var(--color-sun)', num: 'var(--color-ink)' },
  mint: { fill: 'var(--color-mint)', num: 'var(--color-ink)' },
  paper: { fill: 'var(--color-paper-2)', num: 'var(--color-ink)' },
};

interface BallProps {
  n: number | string;
  color?: BallColor;
  className?: string;
  /** Provide when the ball carries meaning; otherwise it is decorative and hidden from assistive tech. */
  label?: string;
}

/** A numbered lottery ball. Flat colour, ink outline, a white window for the numeral, one highlight stroke. */
export function Ball({ n, color = 'tomato', className, label }: BallProps) {
  const { fill, num } = fills[color];
  const text = String(n);
  return (
    <svg
      viewBox="0 0 64 64"
      className={cx('shrink-0', className ?? 'size-16')}
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      <circle cx="32" cy="32" r="29" fill={fill} stroke="var(--color-ink)" strokeWidth="3" />
      <circle cx="32" cy="32" r="16.5" fill="var(--color-paper-2)" stroke="var(--color-ink)" strokeWidth="2.5" />
      <path d="M13 24a21 21 0 0 1 13-11" fill="none" stroke="var(--color-paper-2)" strokeWidth="4" strokeLinecap="round" opacity="0.75" />
      <text
        x="32"
        y="33"
        textAnchor="middle"
        dominantBaseline="central"
        fontFamily="var(--font-display)"
        fontWeight="800"
        fontSize={text.length > 2 ? 12 : 17}
        fill={num}
      >
        {text}
      </text>
    </svg>
  );
}

const cluster: { n: number; color: BallColor; cls: string; tilt: string; delay: string }[] = [
  { n: 17, color: 'tomato', cls: 'left-[6%] top-[18%] size-24 sm:size-28', tilt: '-8deg', delay: '0s' },
  { n: 42, color: 'sun', cls: 'left-[44%] top-[0%] size-20 sm:size-24', tilt: '6deg', delay: '-1.2s' },
  { n: 8, color: 'cobalt', cls: 'right-[4%] top-[26%] size-24 sm:size-32', tilt: '10deg', delay: '-2.4s' },
  { n: 31, color: 'mint', cls: 'left-[30%] bottom-[2%] size-20 sm:size-24', tilt: '-4deg', delay: '-3.1s' },
  { n: 500, color: 'paper', cls: 'right-[26%] bottom-[8%] size-16 sm:size-20', tilt: '-12deg', delay: '-0.6s' },
];

/** Decorative pile of balls for hero areas. Bobs gently; freezes under reduced motion. */
export function BallCluster({ className }: { className?: string }) {
  return (
    <div className={cx('relative', className ?? 'h-56 w-full sm:h-72')} aria-hidden="true">
      {cluster.map((b) => (
        <div
          key={b.n}
          className={cx('absolute animate-bob', b.cls)}
          style={{ ['--tilt' as string]: b.tilt, animationDelay: b.delay, transform: `rotate(${b.tilt})` }}
        >
          <Ball n={b.n} color={b.color} className="size-full" />
        </div>
      ))}
    </div>
  );
}
