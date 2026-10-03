import { useQuery } from '@tanstack/react-query';
import { api, describeError } from '@/api';
import { queryKeys } from '@/api/queryClient';
import { EventTicket } from '@/features/event/EventTicket';
import { Alert } from '@/ui/Alert';
import { Ball, BallCluster } from '@/ui/Ball';
import { Button, buttonClasses } from '@/ui/Button';
import { Skeleton } from '@/ui/Skeleton';

const STEPS = [
  { n: 1, color: 'sun' as const, title: 'Sign up once', body: 'Confirm your email with a one-time code. One person, one identity.' },
  { n: 2, color: 'tomato' as const, title: 'Enter during the window', body: 'First minute or last minute makes no difference. One entry each, same odds.' },
  { n: 3, color: 'mint' as const, title: 'Watch the draw', body: 'A public draw picks the seats. Winners get a short hold to claim; anyone can verify it.' },
];

function EventList() {
  const { data, error, isPending, refetch, isFetching } = useQuery({
    queryKey: queryKeys.events,
    queryFn: ({ signal }) => api.events.list(signal),
  });

  if (isPending && !error) {
    return (
      <div className="grid gap-8 md:grid-cols-2" aria-busy="true" aria-label="Loading drops">
        <Skeleton className="h-64" />
        <Skeleton className="h-64" />
      </div>
    );
  }

  if (error) {
    const e = describeError(error);
    return (
      <Alert
        tone={e.tone}
        title={e.title}
        action={
          <Button size="sm" variant="secondary" onClick={() => void refetch()} loading={isFetching}>
            Try again
          </Button>
        }
      >
        {e.body}
      </Alert>
    );
  }

  if (data.length === 0) {
    return <Alert title="Nothing on the calendar yet">New drops show up here as soon as they are announced.</Alert>;
  }

  return (
    <ul className="grid gap-8 md:grid-cols-2">
      {data.map((event) => (
        <li key={event.id}>
          <EventTicket event={event} />
        </li>
      ))}
    </ul>
  );
}

export function HomePage() {
  return (
    <div className="space-y-16">
      <section className="halftone -mx-4 grid items-center gap-6 px-4 py-6 sm:-mx-6 sm:px-6 md:grid-cols-[1.15fr_1fr]">
        <div>
          <p className="font-mono text-xs font-semibold uppercase tracking-widest text-tomato-deep">Limited seats, unlimited fairness</p>
          <h1 className="mt-3 font-display text-4xl sm:text-5xl">The drum doesn’t care how fast you click.</h1>
          <p className="mt-4 max-w-lg text-lg text-ink-2">
            Fair Drop hands out seats by lottery. Enter any time while the window is open: being first earns you nothing, and bots get no
            head start. When the window closes, one public draw picks the winners.
          </p>
          <a href="#drops" className={buttonClasses('primary', 'lg', 'mt-6')}>
            See the drops
          </a>
        </div>
        <BallCluster />
      </section>

      <section id="drops" aria-labelledby="drops-h" className="scroll-mt-20">
        <h2 id="drops-h" className="mb-6 font-display text-2xl sm:text-3xl">
          Drops
        </h2>
        <EventList />
      </section>

      <section aria-labelledby="how-h">
        <h2 id="how-h" className="mb-6 font-display text-2xl sm:text-3xl">
          How it works
        </h2>
        <ol className="grid gap-6 sm:grid-cols-3">
          {STEPS.map((s) => (
            <li key={s.n} className="flex gap-4 sm:flex-col">
              <Ball n={s.n} color={s.color} className="size-14 sm:size-16" />
              <div>
                <h3 className="font-display text-lg">{s.title}</h3>
                <p className="mt-1 text-ink-2">{s.body}</p>
              </div>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}
