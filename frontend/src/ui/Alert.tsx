import type { ReactNode } from 'react';
import { AlertTriangle, CheckCircle2, Info, WifiOff } from 'lucide-react';
import { cx } from '@/lib/cx';
import type { Tone } from '@/api/errors';

type AlertTone = Tone | 'success' | 'offline';

const styles: Record<AlertTone, string> = {
  info: 'bg-cobalt-tint',
  warn: 'bg-sun-tint',
  error: 'bg-tomato-tint',
  success: 'bg-mint-tint',
  offline: 'bg-paper-3',
};

const icons: Record<AlertTone, ReactNode> = {
  info: <Info className="size-5" aria-hidden="true" />,
  warn: <AlertTriangle className="size-5" aria-hidden="true" />,
  error: <AlertTriangle className="size-5" aria-hidden="true" />,
  success: <CheckCircle2 className="size-5" aria-hidden="true" />,
  offline: <WifiOff className="size-5" aria-hidden="true" />,
};

interface AlertProps {
  tone?: AlertTone;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}

/**
 * Calm inline message. info/success/warn are polite live regions; only `error`
 * interrupts. Nothing here is red-alert: it is a note from a person, not a siren.
 */
export function Alert({ tone = 'info', title, children, action, className }: AlertProps) {
  return (
    <div
      role={tone === 'error' ? 'alert' : 'status'}
      className={cx('flex gap-3 rounded-md border-2 border-ink p-4', styles[tone], className)}
    >
      <span className="mt-0.5 shrink-0">{icons[tone]}</span>
      <div className="min-w-0 flex-1">
        <p className="font-display text-base font-bold leading-snug">{title}</p>
        {children && <div className="mt-1 text-sm text-ink-2">{children}</div>}
        {action && <div className="mt-3">{action}</div>}
      </div>
    </div>
  );
}
