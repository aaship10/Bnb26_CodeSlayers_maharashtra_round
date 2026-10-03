import { cx } from '@/lib/cx';

/** The mark: a ball dropping into a notch. Reads as a lottery ball first, a ticket cut-out second. */
export function LogoMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 40 40" className={cx('size-9', className)} aria-hidden="true">
      <circle cx="20" cy="20" r="17" fill="var(--color-tomato)" stroke="var(--color-ink)" strokeWidth="2.5" />
      <circle cx="20" cy="20" r="8.5" fill="var(--color-paper-2)" stroke="var(--color-ink)" strokeWidth="2" />
      <path d="M8 14a13 13 0 0 1 8-6" fill="none" stroke="var(--color-paper-2)" strokeWidth="3" strokeLinecap="round" opacity="0.8" />
    </svg>
  );
}

export function Wordmark({ className }: { className?: string }) {
  return (
    <span className={cx('inline-flex items-center gap-2.5 font-display text-xl font-extrabold tracking-tight', className)}>
      <LogoMark />
      <span>
        Fair<span className="text-tomato-deep">Drop</span>
      </span>
    </span>
  );
}
