import { cx } from '@/lib/cx';

/** Placeholder block. Pulses only when motion is allowed. */
export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden="true" className={cx('animate-pulse rounded-md bg-paper-3 motion-reduce:animate-none', className)} />;
}
