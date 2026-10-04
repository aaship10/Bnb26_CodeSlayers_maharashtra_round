import { useMemo } from 'react';

const COLORS = ['var(--color-tomato)', 'var(--color-sun)', 'var(--color-cobalt)', 'var(--color-mint)', 'var(--color-ink)'];

/** Deterministic pseudo-random so the burst looks the same every time (and in tests). */
function seeded(i: number): number {
  const x = Math.sin(i * 9301 + 49297) * 233280;
  return x - Math.floor(x);
}

/**
 * One short burst of paper confetti for a successful claim, and only for that.
 * Decorative (aria-hidden), pointer-transparent, and not rendered at all for
 * people who prefer reduced motion.
 */
export function Confetti({ pieces = 36 }: { pieces?: number }) {
  const bits = useMemo(
    () =>
      Array.from({ length: pieces }, (_, i) => ({
        left: `${Math.round(seeded(i) * 100)}%`,
        delay: `${(seeded(i + 100) * 0.6).toFixed(2)}s`,
        dx: `${Math.round((seeded(i + 200) - 0.5) * 30)}vw`,
        spin: `${Math.round(360 + seeded(i + 300) * 720)}deg`,
        color: COLORS[i % COLORS.length],
        w: 6 + Math.round(seeded(i + 400) * 6),
        round: i % 4 === 0,
      })),
    [pieces],
  );
  return (
    <div className="pointer-events-none fixed inset-0 z-40 overflow-hidden motion-reduce:hidden" aria-hidden="true" data-testid="confetti">
      {bits.map((b, i) => (
        <span
          key={i}
          className="absolute top-0 animate-confetti border border-ink"
          style={{
            left: b.left,
            width: b.w,
            height: b.round ? b.w : b.w * 1.6,
            borderRadius: b.round ? '999px' : '2px',
            background: b.color,
            animationDelay: b.delay,
            ['--dx' as string]: b.dx,
            ['--spin' as string]: b.spin,
          }}
        />
      ))}
    </div>
  );
}
