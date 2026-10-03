import { useLocation } from 'react-router-dom';
import { Ticket } from '@/ui/Ticket';
import { ButtonLink } from '@/ui/Button';
import { Ball } from '@/ui/Ball';

/** Shown on pages that are about "you" when nobody is signed in. Remembers where to come back to. */
export function SignInPrompt({ title, body }: { title: string; body: string }) {
  const location = useLocation();
  return (
    <div className="mx-auto max-w-xl">
      <Ticket
        stub={
          <>
            <span className="text-sm text-ink-2">Takes under a minute</span>
            <ButtonLink to="/register" state={{ from: location.pathname }} size="sm">
              Sign in
            </ButtonLink>
          </>
        }
      >
        <div className="flex items-start gap-4">
          <Ball n="?" color="sun" className="size-14" />
          <div>
            <h1 className="font-display text-2xl">{title}</h1>
            <p className="mt-2 text-ink-2">{body}</p>
          </div>
        </div>
      </Ticket>
    </div>
  );
}
