import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { Link, type LinkProps } from 'react-router-dom';
import { cx } from '@/lib/cx';
import { Spinner } from './Spinner';

type Variant = 'primary' | 'secondary' | 'sun' | 'ghost';
type Size = 'sm' | 'md' | 'lg';

const base =
  'relative inline-flex select-none items-center justify-center gap-2 whitespace-nowrap font-display font-semibold ' +
  'border-2 border-ink rounded-md transition-[transform,box-shadow,background-color] duration-100 ' +
  'motion-reduce:transition-none';

const variants: Record<Variant, string> = {
  primary: 'bg-tomato text-white shadow-pop hover:bg-tomato-deep',
  secondary: 'bg-paper-2 text-ink shadow-pop hover:bg-sun-tint',
  sun: 'bg-sun text-ink shadow-pop hover:brightness-95',
  ghost: 'border-transparent bg-transparent text-ink hover:border-ink hover:bg-paper-2',
};

// Raised buttons press into their shadow; ghost buttons have nothing to press.
const raised = 'hover:-translate-y-0.5 active:translate-y-1 active:shadow-none';

const sizes: Record<Size, string> = {
  sm: 'min-h-10 px-3.5 text-sm',
  md: 'min-h-12 px-5 text-base',
  lg: 'min-h-14 px-7 text-lg',
};

const disabled =
  'disabled:border-ink-3 disabled:bg-paper-3 disabled:text-ink-3 disabled:shadow-none ' +
  'disabled:translate-y-0 disabled:hover:bg-paper-3';

export function buttonClasses(variant: Variant = 'primary', size: Size = 'md', extra?: string): string {
  return cx(base, variants[variant], variant !== 'ghost' && raised, sizes[size], disabled, extra);
}

interface CommonProps {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  leading?: ReactNode;
  trailing?: ReactNode;
}

export type ButtonProps = CommonProps & ButtonHTMLAttributes<HTMLButtonElement>;

/**
 * `loading` keeps the label in place (so the button doesn't resize), swaps in a
 * spinner, and disables the control. Use it for "pending" so a double tap can't
 * send two requests.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'primary', size = 'md', loading, leading, trailing, className, children, disabled: isDisabled, type = 'button', ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      disabled={isDisabled || loading}
      aria-busy={loading || undefined}
      className={buttonClasses(variant, size, className)}
      {...rest}
    >
      {loading ? <Spinner className="size-5" /> : leading}
      <span>{children}</span>
      {!loading && trailing}
    </button>
  );
});

export type ButtonLinkProps = CommonProps & LinkProps;

/** A router link that looks like a button (navigation, not an action). */
export function ButtonLink({ variant = 'primary', size = 'md', leading, trailing, className, children, ...rest }: ButtonLinkProps) {
  return (
    <Link className={buttonClasses(variant, size, className)} {...rest}>
      {leading}
      <span>{children}</span>
      {trailing}
    </Link>
  );
}
