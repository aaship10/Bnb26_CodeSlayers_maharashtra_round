import { useLocation } from 'react-router-dom';
import { describeError } from '@/api';
import type { EventInfo, StatusResponse } from '@/api/schemas';
import { ChallengePanel } from '@/features/challenge/ChallengePanel';
import { useChallenge } from '@/features/challenge/useChallenge';
import { useSession } from '@/state/session';
import { Alert } from '@/ui/Alert';
import { Button, ButtonLink } from '@/ui/Button';
import { Skeleton } from '@/ui/Skeleton';
import { deriveEnterView } from './enterView';
import { useEnter } from './useEnter';
import { useCountdownSeconds } from '@/lib/useCountdownSeconds';

interface Props {
  event: EventInfo;
  status: StatusResponse | undefined;
  statusLoading: boolean;
}

export function EnterCard({ event, status, statusLoading }: Props) {
  const session = useSession();
  const location = useLocation();
  const challenge = useChallenge();
  const enter = useEnter(event.id, challenge.handler);

  const view = deriveEnterView({
    phase: event.phase,
    signedIn: !!session,
    justEntered: enter.phase === 'done',
    statusLoading,
    statusState: status?.state,
  });

  const err = enter.error ? describeError(enter.error) : null;
  const waitS = useCountdownSeconds(err?.code === 'RATE_LIMITED' ? err.retryAfterMs : undefined, enter.error);
  const rateLimited = err?.code === 'RATE_LIMITED';
  const blocked = rateLimited && waitS > 0;
  const busy = enter.phase === 'sending' || enter.phase === 'retrying';
  const challenging = challenge.state.kind !== 'idle';
  const signInState = { from: location.pathname };
  const statusPath = `/events/${encodeURIComponent(event.id)}/status`;

  switch (view) {
    case 'draft':
      return <Alert title="Not announced yet">This drop is still being set up. Check back soon.</Alert>;

    case 'not_open_yet':
      return (
        <div className="space-y-3">
          <Button size="lg" className="w-full sm:w-auto" disabled>
            Entries open soon
          </Button>
          <p className="text-sm text-ink-2">
            The button wakes up when the window opens. No need to be here at the first second.
          </p>
          {!session && (
            <ButtonLink to="/register" state={signInState} variant="secondary" size="sm">
              Sign in ahead of time
            </ButtonLink>
          )}
        </div>
      );

    case 'sign_in':
      return (
        <div className="space-y-3">
          <ButtonLink to="/register" state={signInState} size="lg" className="w-full sm:w-auto">
            Sign in to enter
          </ButtonLink>
          <p className="text-sm text-ink-2">It takes under a minute: an email and a one-time code.</p>
        </div>
      );

    case 'checking':
      return (
        <div aria-busy="true" aria-label="Checking your entry">
          <Skeleton className="h-14 w-56" />
        </div>
      );

    case 'entered':
      return (
        <Alert
          tone="success"
          title="You’re in the draw"
          action={
            <ButtonLink to={statusPath} size="sm" variant="secondary">
              See my status
            </ButtonLink>
          }
        >
          {enter.result?.already_entered ? 'You had already entered, so nothing changed. ' : ''}
          Nothing more to do until the window closes and the draw runs. You can close this tab; your entry is saved.
        </Alert>
      );

    case 'view_status':
      return (
        <Alert
          title="You took part in this drop"
          action={
            <ButtonLink to={statusPath} size="sm">
              See my status
            </ButtonLink>
          }
        >
          Your result lives on your status page.
        </Alert>
      );

    case 'closed':
      return (
        <Alert title="Entries are closed">
          {event.phase === 'DRAWING' ? 'The draw is running right now.' : 'This drop is no longer taking entries.'}
        </Alert>
      );

    case 'can_enter':
      break;
  }

  // can_enter
  const label = enter.phase === 'retrying' ? 'Trying again…' : busy ? 'Entering…' : blocked ? `Try again in ${waitS}s` : rateLimited ? 'Try again' : 'Enter the draw';

  return (
    <div className="space-y-4">
      {!challenging && (
        <Button size="lg" className="w-full sm:w-auto sm:min-w-64" onClick={() => void enter.enter()} loading={busy} disabled={blocked}>
          {label}
        </Button>
      )}

      <ChallengePanel state={challenge.state} onCaptcha={challenge.submitCaptcha} onCancel={enter.cancel} />

      {enter.phase === 'retrying' && (
        <p role="status" className="text-sm text-ink-2">
          Your connection hiccupped. Trying again, your entry won’t be doubled.
        </p>
      )}

      {err && (
        <Alert tone={err.tone} title={err.title}>
          {blocked ? `We’ll let you try again in ${waitS} second${waitS === 1 ? '' : 's'}.` : err.body}
        </Alert>
      )}

      {!err && !busy && !challenging && <p className="text-sm text-ink-2">One tap. You can only enter once, and that’s all it takes.</p>}
    </div>
  );
}
