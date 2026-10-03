import { useEffect, useId, useRef, type ReactNode, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { cx } from '@/lib/cx';

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

interface DialogProps {
  open: boolean;
  title: string;
  children: ReactNode;
  /** Omit to make the dialog non-dismissible (no Esc, no close button). */
  onClose?: () => void;
  /** Element to focus first; defaults to the first focusable element. */
  initialFocus?: RefObject<HTMLElement | null>;
  size?: 'sm' | 'md';
}

/**
 * Accessible modal: role=dialog + aria-modal, focus moves in and is trapped,
 * Esc closes (when dismissible), the page behind is inert, and focus returns to
 * whatever opened it. Rendered in a portal so it sits above everything.
 */
export function Dialog({ open, title, children, onClose, initialFocus, size = 'sm' }: DialogProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const root = document.getElementById('root') as (HTMLElement & { inert?: boolean }) | null;
    if (root) root.inert = true;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    const panel = panelRef.current;
    const first = initialFocus?.current ?? panel?.querySelector<HTMLElement>(FOCUSABLE) ?? panel;
    first?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && onCloseRef.current) {
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (e.key !== 'Tab' || !panel) return;
      const items = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)];
      if (items.length === 0) {
        e.preventDefault();
        return;
      }
      const firstEl = items[0]!;
      const lastEl = items[items.length - 1]!;
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    };
    document.addEventListener('keydown', onKey, true);

    return () => {
      document.removeEventListener('keydown', onKey, true);
      if (root) root.inert = false;
      document.body.style.overflow = prevOverflow;
      previous?.focus?.();
    };
  }, [open, initialFocus]);

  if (!open) return null;

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-ink/40 p-3 sm:items-center sm:p-6">
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={cx(
          'max-h-[90dvh] w-full overflow-y-auto rounded-lg border-2 border-ink bg-paper-2 p-5 shadow-pop-lg outline-none sm:p-6',
          size === 'sm' ? 'max-w-md' : 'max-w-2xl',
        )}
      >
        <div className="mb-3 flex items-start justify-between gap-4">
          <h2 id={titleId} className="font-display text-xl">
            {title}
          </h2>
          {onClose && (
            <button type="button" onClick={onClose} aria-label="Close" className="-m-1 rounded p-1 text-ink-2 hover:bg-paper-3">
              <X className="size-5" aria-hidden="true" />
            </button>
          )}
        </div>
        {children}
      </div>
    </div>,
    document.body,
  );
}
