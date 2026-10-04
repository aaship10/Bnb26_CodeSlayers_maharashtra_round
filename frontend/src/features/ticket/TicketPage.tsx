import { useEffect, useRef } from 'react';
import { useLocation, useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Printer } from 'lucide-react';
import { api, describeError } from '@/api';
import { queryKeys } from '@/api/queryClient';
import type { ClaimResponse } from '@/api/schemas';
import { useDocumentTitle } from '@/lib/useDocumentTitle';
import { useSession } from '@/state/session';
import { Alert } from '@/ui/Alert';
import { Ball } from '@/ui/Ball';
import { Button, ButtonLink } from '@/ui/Button';
import { Skeleton } from '@/ui/Skeleton';
import { Ticket } from '@/ui/Ticket';
import { SignInPrompt } from '@/features/auth/SignInPrompt';
import { claimResultKey } from '@/features/claim/useClaim';
import { Confetti } from './Confetti';

/** Groups a code for reading aloud at a door: FD-7K3Q-9XMP stays as is; anything else is shown verbatim. */
function CodeBlock({ code }: { code: string }) {
  return (
    <p className="select-all break-all font-mono text-2xl font-bold tracking-[0.18em] sm:text-3xl" data-testid="ticket-code">
      {code}
    </p>
  );
}

export function TicketPage() {
  const { id = '' } = useParams();
  const session = useSession();
  const location = useLocation();
  const queryClient = useQueryClient();
  const justClaimed = (location.state as { justClaimed?: boolean } | null)?.justClaimed === true;
  useDocumentTitle('Your ticket');

  const eventQuery = useQuery({ queryKey: queryKeys.event(id), queryFn: ({ signal }) => api.events.get(id, signal), enabled: !!id, staleTime: 60_000 });
  const statusQuery = useQuery({
    queryKey: queryKeys.status(id),
    queryFn: ({ signal }) => api.events.status(id, signal),
    enabled: !!session && !!id,
    staleTime: 30_000,
  });
  const meQuery = useQuery({ queryKey: queryKeys.me, queryFn: ({ signal }) => api.auth.me(signal), enabled: !!session, staleTime: 10 * 60_000 });

  // Move focus to the heading on arrival so screen-reader users land on the news.
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => headingRef.current?.focus(), [statusQuery.data?.state]);

  if (!session) return <SignInPrompt title="Sign in to see your ticket" body="Tickets live in your account." />;

  const status = statusQuery.data;
  if (!status) {
    if (statusQuery.error) {
      const e = describeError(statusQuery.error);
      return (
        <div className="mx-auto max-w-xl">
          <Alert tone={e.tone} title={e.title}>
            {e.body}
          </Alert>
        </div>
      );
    }
    return <Skeleton className="mx-auto h-80 max-w-2xl" />;
  }

  const statusPath = `/events/${encodeURIComponent(id)}/status`;
  if (status.state !== 'CLAIMED') {
    return (
      <div className="mx-auto max-w-xl space-y-4">
        <Alert title="No ticket here yet">A ticket appears once you’ve claimed a seat.</Alert>
        <ButtonLink to={statusPath} variant="secondary">
          See my status
        </ButtonLink>
      </div>
    );
  }

  // The status carries seat and code (requested from A); the claim response in the cache is the fallback.
  const claimed = queryClient.getQueryData<ClaimResponse>(claimResultKey(id));
  const seat = status.seat_no ?? claimed?.seat_no;
  const code = status.ticket_code ?? claimed?.ticket_code;
  const event = eventQuery.data;

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      {justClaimed && <Confetti />}

      <div className="text-center">
        <h1 ref={headingRef} tabIndex={-1} className="font-display text-3xl outline-none sm:text-4xl">
          {justClaimed ? 'It’s yours. Enjoy the show.' : 'Your ticket'}
        </h1>
        {justClaimed && <p className="mt-2 text-ink-2">Your seat is confirmed and saved to your account.</p>}
      </div>

      <Ticket
        tone="paper"
        stub={
          <>
            <span className="font-mono text-xs uppercase tracking-widest text-ink-3">Admit one</span>
            <span className="font-mono text-sm font-bold">{code ?? 'code on its way'}</span>
          </>
        }
      >
        <div className="flex flex-col gap-6 sm:flex-row sm:items-center">
          <div className="flex-1 space-y-4">
            <div>
              <p className="font-mono text-xs font-semibold uppercase tracking-widest text-tomato-deep">Fair Drop ticket</p>
              <h2 className="mt-1 font-display text-2xl sm:text-3xl">{event?.name ?? 'Your event'}</h2>
            </div>
            {meQuery.data && (
              <div>
                <p className="text-sm text-ink-3">Name</p>
                <p className="font-display text-lg font-bold">{meQuery.data.display_name}</p>
              </div>
            )}
            <div>
              <p className="text-sm text-ink-3">Ticket code</p>
              {code ? (
                <CodeBlock code={code} />
              ) : (
                <p className="text-ink-2">Your code will appear here shortly. Your seat is already confirmed.</p>
              )}
            </div>
          </div>
          {seat !== undefined && (
            <div className="flex flex-col items-center gap-1">
              <Ball n={seat} color="tomato" className="size-28 sm:size-32" label={`Seat ${seat}`} />
              <span className="font-display text-sm font-bold uppercase tracking-widest">Seat</span>
            </div>
          )}
        </div>
      </Ticket>

      <div className="no-print flex flex-wrap items-center justify-center gap-3">
        <Button variant="secondary" leading={<Printer className="size-5" aria-hidden="true" />} onClick={() => window.print()}>
          Print
        </Button>
        <ButtonLink to={statusPath} variant="ghost">
          Back to status
        </ButtonLink>
      </div>
    </div>
  );
}
