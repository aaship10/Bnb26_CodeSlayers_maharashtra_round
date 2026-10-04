import { cx } from '@/lib/cx';

interface Props {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
  className?: string;
}

/** On/off switch. A real button with role=switch, so it is keyboard and screen-reader native. */
export function Switch({ checked, onChange, label, disabled, className }: Props) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cx(
        'relative inline-flex h-7 w-12 shrink-0 items-center rounded-full border-2 border-ink transition-colors motion-reduce:transition-none',
        checked ? 'bg-pine' : 'bg-paper-3',
        'disabled:cursor-not-allowed disabled:opacity-50',
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cx(
          'inline-block size-5 rounded-full border-2 border-ink bg-paper-2 transition-transform motion-reduce:transition-none',
          checked ? 'translate-x-[1.375rem]' : 'translate-x-0.5',
        )}
      />
    </button>
  );
}
