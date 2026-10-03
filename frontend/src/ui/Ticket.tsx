import type { CSSProperties, ElementType, ReactNode } from 'react';
import { cx } from '@/lib/cx';

interface TicketProps {
  children: ReactNode;
  /** Bottom stub below a dashed perforation. Keep it short: its height is fixed so the notches line up. */
  stub?: ReactNode;
  /** Background of the main area. */
  tone?: 'paper' | 'sun' | 'mint' | 'tomato';
  className?: string;
  as?: ElementType;
  /** Lift on hover; set when the whole ticket is a target. */
  interactive?: boolean;
}

const tones: Record<NonNullable<TicketProps['tone']>, string> = {
  paper: 'var(--color-paper-2)',
  sun: 'var(--color-sun-tint)',
  mint: 'var(--color-mint-tint)',
  tomato: 'var(--color-tomato-tint)',
};

/**
 * The signature surface: a paper ticket with semicircle notches and a dashed tear line.
 * The geometry lives in styles/components.css.
 */
export function Ticket({ children, stub, tone = 'paper', className, as: Tag = 'div', interactive }: TicketProps) {
  return (
    <Tag
      className={cx('ticket', className)}
      data-stub={stub ? 'true' : 'false'}
      data-interactive={interactive ? 'true' : undefined}
      style={{ '--ticket-bg': tones[tone] } as CSSProperties}
    >
      <div className="ticket-shell">
        <div className="ticket-body">
          <div className="p-5 sm:p-6">{children}</div>
          {stub && <div className="ticket-stub">{stub}</div>}
        </div>
      </div>
    </Tag>
  );
}
