import { useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { cx } from '@/lib/cx';

/**
 * A full hash, wrapped (never silently truncated: people compare these by eye),
 * selectable, with a copy button. Clipboard needs a secure context; when it's
 * missing the value is still select-all.
 */
export function HashValue({ value, label, className }: { value: string; label: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* no clipboard (insecure origin); the text is still selectable */
    }
  };
  return (
    <span className={cx('flex items-start gap-2', className)}>
      <code className="min-w-0 select-all break-all rounded-sm bg-paper-3 px-1.5 py-0.5 font-mono text-xs leading-relaxed text-ink">{value}</code>
      <button type="button" onClick={() => void copy()} aria-label={`Copy ${label}`} className="mt-0.5 shrink-0 text-ink-3 hover:text-ink">
        {copied ? <Check className="size-4 text-pine" aria-hidden="true" /> : <Copy className="size-4" aria-hidden="true" />}
      </button>
    </span>
  );
}
