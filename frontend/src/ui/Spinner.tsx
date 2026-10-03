import { cx } from '@/lib/cx';

/** A ring that turns; with reduced motion the global rule freezes it and the label still says what is happening. */
export function Spinner({ className, label }: { className?: string; label?: string }) {
  return (
    <span role={label ? 'status' : undefined} className="inline-flex items-center">
      <svg viewBox="0 0 24 24" className={cx('animate-spin-slow', className ?? 'size-6')} aria-hidden="true" fill="none">
        <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" opacity="0.25" />
        <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
      </svg>
      {label && <span className="sr-only">{label}</span>}
    </span>
  );
}
