import { forwardRef, useId, type InputHTMLAttributes, type ReactNode } from 'react';
import { cx } from '@/lib/cx';

interface FieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  hint?: ReactNode;
  error?: string;
}

/** Labelled text input. The label is always visible (placeholders are not labels). */
export const Field = forwardRef<HTMLInputElement, FieldProps>(function Field({ label, hint, error, className, id, ...rest }, ref) {
  const auto = useId();
  const inputId = id ?? auto;
  const hintId = hint ? `${inputId}-hint` : undefined;
  const errId = error ? `${inputId}-err` : undefined;
  return (
    <div className={className}>
      <label htmlFor={inputId} className="mb-1.5 block font-display text-sm font-bold">
        {label}
      </label>
      <input
        ref={ref}
        id={inputId}
        aria-invalid={error ? true : undefined}
        aria-describedby={[hintId, errId].filter(Boolean).join(' ') || undefined}
        className={cx(
          'min-h-12 w-full rounded-md border-2 bg-paper-2 px-3.5 text-base text-ink placeholder:text-ink-3',
          'shadow-[inset_2px_2px_0_0_var(--color-rule)] transition-colors',
          'focus:border-cobalt focus:outline-none focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-cobalt',
          'disabled:cursor-not-allowed disabled:bg-paper-3 disabled:text-ink-3',
          error ? 'border-tomato-deep' : 'border-ink',
        )}
        {...rest}
      />
      {hint && (
        <p id={hintId} className="mt-1.5 text-sm text-ink-3">
          {hint}
        </p>
      )}
      {error && (
        <p id={errId} className="mt-1.5 text-sm font-semibold text-tomato-deep">
          {error}
        </p>
      )}
    </div>
  );
});
