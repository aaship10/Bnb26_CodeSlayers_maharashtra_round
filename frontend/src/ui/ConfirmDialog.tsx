import { useEffect, useState, type ReactNode } from 'react';
import { describeError } from '@/api/errors';
import { Alert } from './Alert';
import { Button } from './Button';
import { Dialog } from './Dialog';
import { Field } from './Field';

interface Props {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  /** Danger styling for destructive actions. */
  danger?: boolean;
  /** Typed confirmation: the button stays off until this exact text is entered. */
  requireText?: string;
  onConfirm: () => Promise<unknown>;
  onClose: () => void;
}

/**
 * "Are you sure?" with teeth: the action runs inside the dialog, its error is
 * shown inside the dialog, and the dialog only closes on success. Nothing
 * important happens on a single click.
 */
export function ConfirmDialog({ open, title, children, confirmLabel, danger, requireText, onConfirm, onClose }: Props) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [typed, setTyped] = useState('');

  useEffect(() => {
    if (open) {
      setError(null);
      setTyped('');
      setPending(false);
    }
  }, [open]);

  const ready = !requireText || typed.trim() === requireText;
  const run = async () => {
    if (!ready || pending) return;
    setPending(true);
    setError(null);
    try {
      await onConfirm();
      onClose();
    } catch (e) {
      setError(e);
    } finally {
      setPending(false);
    }
  };
  const e = error ? describeError(error) : null;

  return (
    <Dialog open={open} title={title} onClose={pending ? undefined : onClose}>
      <div className="space-y-4 text-ink-2">
        <div>{children}</div>
        {requireText && (
          <Field
            label={`Type ${requireText} to confirm`}
            value={typed}
            onChange={(ev) => setTyped(ev.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        )}
        {e && (
          <Alert tone={e.tone} title={e.title}>
            {e.detail ?? e.body}
          </Alert>
        )}
        <div className="flex flex-wrap justify-end gap-3 pt-1">
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button
            variant={danger ? 'primary' : 'sun'}
            onClick={() => void run()}
            loading={pending}
            disabled={!ready}
            className={danger ? 'bg-tomato-deep hover:bg-ink' : undefined}
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
