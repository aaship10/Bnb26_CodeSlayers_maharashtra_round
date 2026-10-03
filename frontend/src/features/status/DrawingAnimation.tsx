import { Ball, type BallColor } from '@/ui/Ball';

// Arranged around the axle so nothing hides behind it.
const BALLS: { n: number; color: BallColor; x: string; y: string }[] = [
  { n: 7, color: 'tomato', x: '10%', y: '14%' },
  { n: 42, color: 'sun', x: '56%', y: '8%' },
  { n: 13, color: 'cobalt', x: '60%', y: '56%' },
  { n: 88, color: 'mint', x: '12%', y: '58%' },
];

/** A lottery drum turning. Decorative; under reduced motion it simply stands still. */
export function DrawingAnimation() {
  return (
    <div className="relative mx-auto size-40 shrink-0 sm:size-48" aria-hidden="true">
      <div className="absolute inset-0 rounded-full border-[3px] border-ink bg-paper-2 shadow-pop" />
      <div className="absolute inset-[6px] animate-tumble rounded-full">
        {BALLS.map((b) => (
          <div key={b.n} className="absolute size-12 sm:size-14" style={{ left: b.x, top: b.y }}>
            <Ball n={b.n} color={b.color} className="size-full" />
          </div>
        ))}
      </div>
      {/* the drum's axle */}
      <div className="absolute left-1/2 top-1/2 size-4 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-ink bg-ink-2" />
    </div>
  );
}
