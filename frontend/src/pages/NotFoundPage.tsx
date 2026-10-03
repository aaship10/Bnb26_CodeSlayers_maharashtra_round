import { Ticket } from '@/ui/Ticket';
import { Ball } from '@/ui/Ball';
import { ButtonLink } from '@/ui/Button';

export function NotFoundPage() {
  return (
    <div className="mx-auto max-w-xl">
      <Ticket
        stub={
          <>
            <span className="font-mono text-xs uppercase tracking-widest text-ink-3">Error 404</span>
            <ButtonLink to="/" size="sm">
              Back to drops
            </ButtonLink>
          </>
        }
      >
        <div className="flex items-start gap-4">
          <Ball n={404} color="tomato" className="size-14" />
          <div>
            <h1 className="font-display text-2xl">That ball isn’t in the drum</h1>
            <p className="mt-2 text-ink-2">We couldn’t find this page. It may have moved, or the link may have a typo.</p>
          </div>
        </div>
      </Ticket>
    </div>
  );
}
