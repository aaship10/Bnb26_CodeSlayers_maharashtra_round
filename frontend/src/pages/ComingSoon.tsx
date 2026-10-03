import { Ticket } from '@/ui/Ticket';
import { Ball } from '@/ui/Ball';
import { ButtonLink } from '@/ui/Button';

/** Honest placeholder for routes whose stage hasn't been built yet. Replaced stage by stage. */
export function ComingSoon({ title, stage, children }: { title: string; stage: number; children?: string }) {
  return (
    <div className="mx-auto max-w-xl">
      <Ticket
        stub={
          <>
            <span className="font-mono text-xs uppercase tracking-widest text-ink-3">Stage {stage} of 7</span>
            <ButtonLink to="/" variant="secondary" size="sm">
              Back to drops
            </ButtonLink>
          </>
        }
      >
        <div className="flex items-start gap-4">
          <Ball n={stage} color="sun" className="size-14" />
          <div>
            <h1 className="font-display text-2xl">{title}</h1>
            <p className="mt-2 text-ink-2">{children ?? 'This screen is wired into the router and arrives in a later build stage.'}</p>
          </div>
        </div>
      </Ticket>
    </div>
  );
}
