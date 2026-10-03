import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Check } from 'lucide-react';
import { queryKeys } from '@/api/queryClient';
import { PHASES, type Phase } from '@/api/schemas';
import { cx } from '@/lib/cx';
import { formatCount, formatTime } from '@/lib/format';
import { uuidv4 } from '@/lib/ids';
import { Button } from '@/ui/Button';
import { ConfirmDialog } from '@/ui/ConfirmDialog';
import { adminApi, adminKeys } from './adminApi';
import { ACTION_FROM, actionEnabled } from './logic';
import { LIFECYCLE, type AdminEvent, type LifecycleAction } from './schemas';

const PHASE_NAME: Record<Phase, string> = {
  DRAFT: 'Draft',
  SCHEDULED: 'Scheduled',
  OPEN: 'Open',
  DRAWING: 'Drawing',
  CLAIMING: 'Claiming',
  CLOSED: 'Closed',
};

const ACTION_LABEL: Record<LifecycleAction, string> = {
  schedule: 'Schedule',
  open: 'Open window',
  close: 'Close window',
  draw: 'Run draw',
};

function confirmCopy(action: LifecycleAction, ev: AdminEvent, entrants: number | undefined) {
  switch (action) {
    case 'schedule':
      return {
        title: 'Publish this event?',
        body: `Attendees will see “${ev.name}” with a countdown to ${formatTime(ev.window_opens_at)}.`,
        confirm: 'Publish',
      };
    case 'open':
      return {
        title: 'Open the entry window now?',
        body: 'Attendees can enter straight away. Opening by hand overrides the scheduled time.',
        confirm: 'Open window',
      };
    case 'close':
      return {
        title: 'Close the entry window?',
        body: 'No more entries will be accepted. This can’t be undone.',
        confirm: 'Close window',
      };
    case 'draw':
      return {
        title: 'Run the draw?',
        body: `Picks ${formatCount(ev.inventory)} winners${entrants !== undefined ? ` from ${formatCount(entrants)} entries` : ''} with the committed seed and the public beacon. The result is final and published on the fairness page.`,
        confirm: 'Run draw',
      };
  }
}

/**
 * Where the event is, and the single next step. Each action is enabled only in
 * the phase it belongs to, always asks for confirmation, and sends a fresh
 * Idempotency-Key per confirmation (reused if that confirmation is retried).
 */
export function PhaseTimeline({ event, entrants }: { event: AdminEvent; entrants?: number }) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<{ action: LifecycleAction; key: string } | null>(null);
  const current = PHASES.indexOf(event.phase);

  const run = async () => {
    if (!pending) return;
    const updated = await adminApi.transition(event.id, pending.action, pending.key);
    queryClient.setQueryData(adminKeys.event(event.id), updated);
    void queryClient.invalidateQueries({ queryKey: adminKeys.events, exact: true });
    void queryClient.invalidateQueries({ queryKey: adminKeys.stats(event.id) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.event(event.id), exact: true });
  };

  const copy = pending ? confirmCopy(pending.action, event, entrants) : null;

  return (
    <section aria-labelledby="lifecycle-h" className="rounded-lg border-2 border-ink bg-paper-2 p-4 sm:p-5">
      <h2 id="lifecycle-h" className="font-display text-xl">
        Lifecycle
      </h2>

      <ol className="mt-4 grid grid-cols-3 gap-y-4 sm:grid-cols-6" aria-label="Phases">
        {PHASES.map((p, i) => {
          const done = i < current;
          const now = i === current;
          return (
            <li key={p} className="flex flex-col items-center text-center" aria-current={now ? 'step' : undefined}>
              <span
                className={cx(
                  'grid size-9 place-items-center rounded-full border-2 border-ink font-display text-sm font-bold',
                  now ? 'bg-tomato text-white shadow-pop-sm' : done ? 'bg-ink text-paper' : 'bg-paper-2 text-ink-3',
                )}
              >
                {done ? <Check className="size-4" aria-hidden="true" /> : i + 1}
              </span>
              <span className={cx('mt-1.5 text-sm', now ? 'font-bold' : 'text-ink-2')}>{PHASE_NAME[p]}</span>
              <span className="sr-only">{done ? '(done)' : now ? '(current)' : ''}</span>
            </li>
          );
        })}
      </ol>

      <div className="mt-5 flex flex-wrap gap-2 border-t-2 border-dashed border-rule pt-4">
        {LIFECYCLE.map((action) => {
          const enabled = actionEnabled(action, event.phase);
          return (
            <Button
              key={action}
              variant={enabled ? 'primary' : 'secondary'}
              size="sm"
              disabled={!enabled}
              onClick={() => setPending({ action, key: uuidv4() })}
              title={enabled ? undefined : `Available when the event is ${PHASE_NAME[ACTION_FROM[action]]}`}
            >
              {action === 'draw' && event.mode === 'FCFS' ? 'Finalise' : ACTION_LABEL[action]}
            </Button>
          );
        })}
      </div>
      <p className="mt-2 text-sm text-ink-3">
        {event.phase === 'CLAIMING'
          ? 'Claiming ends by itself when the holds run out.'
          : event.phase === 'CLOSED'
            ? 'This event is finished.'
            : 'Only the next step is available. Every step asks first.'}
      </p>

      {copy && (
        <ConfirmDialog open title={copy.title} confirmLabel={copy.confirm} danger={pending?.action === 'close' || pending?.action === 'draw'} onConfirm={run} onClose={() => setPending(null)}>
          {copy.body}
        </ConfirmDialog>
      )}
    </section>
  );
}
