import { useState, type ReactNode } from 'react';
import { ArrowRight, Ticket as TicketIcon } from 'lucide-react';
import { PHASES } from '@/api/schemas';
import { Alert } from '@/ui/Alert';
import { Badge, MockBadge, PhaseBadge } from '@/ui/Badge';
import { Ball, BallCluster } from '@/ui/Ball';
import { Button } from '@/ui/Button';
import { Field } from '@/ui/Field';
import { Spinner } from '@/ui/Spinner';
import { Skeleton } from '@/ui/Skeleton';
import { Ticket } from '@/ui/Ticket';
import { Wordmark } from '@/ui/Logo';
import { ChallengePanel } from '@/features/challenge/ChallengePanel';
import { Countdown } from '@/features/event/Countdown';

const SWATCHES: { name: string; cls: string; text: string }[] = [
  { name: 'paper', cls: 'bg-paper', text: 'text-ink' },
  { name: 'paper-2', cls: 'bg-paper-2', text: 'text-ink' },
  { name: 'paper-3', cls: 'bg-paper-3', text: 'text-ink' },
  { name: 'ink', cls: 'bg-ink', text: 'text-paper' },
  { name: 'ink-2', cls: 'bg-ink-2', text: 'text-paper' },
  { name: 'ink-3', cls: 'bg-ink-3', text: 'text-white' },
  { name: 'tomato', cls: 'bg-tomato', text: 'text-white' },
  { name: 'tomato-deep', cls: 'bg-tomato-deep', text: 'text-white' },
  { name: 'tomato-tint', cls: 'bg-tomato-tint', text: 'text-tomato-deep' },
  { name: 'cobalt', cls: 'bg-cobalt', text: 'text-white' },
  { name: 'cobalt-tint', cls: 'bg-cobalt-tint', text: 'text-cobalt' },
  { name: 'sun', cls: 'bg-sun', text: 'text-ink' },
  { name: 'sun-tint', cls: 'bg-sun-tint', text: 'text-ink' },
  { name: 'mint', cls: 'bg-mint', text: 'text-ink' },
  { name: 'pine', cls: 'bg-pine', text: 'text-white' },
  { name: 'mint-tint', cls: 'bg-mint-tint', text: 'text-pine' },
];

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-5">
      <h2 className="border-b-2 border-ink pb-2 font-display text-2xl">{title}</h2>
      {children}
    </section>
  );
}

/** Dev-only. Review the look in one place before it's used on real screens. */
export function Styleguide() {
  const [loading, setLoading] = useState(false);

  return (
    <div className="space-y-14">
      <header>
        <Wordmark />
        <h1 className="mt-4 font-display text-4xl">Styleguide</h1>
        <p className="mt-2 max-w-xl text-ink-2">Tokens and components. Dev builds only; this route does not ship.</p>
      </header>

      <Section title="Colour">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {SWATCHES.map((s) => (
            <div key={s.name} className={`${s.cls} ${s.text} rounded-md border-2 border-ink p-3`}>
              <p className="font-mono text-xs font-bold">{s.name}</p>
            </div>
          ))}
        </div>
      </Section>

      <Section title="Type">
        <div className="space-y-3">
          <p className="font-display text-5xl">Display 5xl, Bricolage</p>
          <p className="font-display text-3xl">Heading 3xl: Everyone gets one ticket</p>
          <p className="font-display text-xl">Heading xl: The window closes at 10:30</p>
          <p className="max-w-prose text-base">
            Body, DM Sans 16/25. Enter any time while the window is open. First minute or last minute makes no difference; every identity
            gets exactly one entry, and the draw is public.
          </p>
          <p className="text-sm text-ink-2">Small, secondary text. Hold expires in 09:42.</p>
          <p className="font-mono text-sm">FD-7K3Q-9XMP · p_3fa91c20d4be · 4f9a…c0e1</p>
          <p className="tnum font-display text-4xl">09:42:17</p>
        </div>
      </Section>

      <Section title="Buttons">
        <div className="flex flex-wrap items-center gap-4">
          <Button>Enter the draw</Button>
          <Button variant="secondary">View drop</Button>
          <Button variant="sun" leading={<TicketIcon className="size-5" aria-hidden="true" />}>
            Claim seat
          </Button>
          <Button variant="ghost" trailing={<ArrowRight className="size-5" aria-hidden="true" />}>
            Ghost
          </Button>
          <Button disabled>Disabled</Button>
          <Button
            loading={loading}
            onClick={() => {
              setLoading(true);
              setTimeout(() => setLoading(false), 1600);
            }}
          >
            {loading ? 'Entering…' : 'Click to load'}
          </Button>
        </div>
        <div className="flex flex-wrap items-center gap-4">
          <Button size="sm">Small</Button>
          <Button size="md">Medium</Button>
          <Button size="lg">Large</Button>
        </div>
      </Section>

      <Section title="Badges">
        <div className="flex flex-wrap items-center gap-3">
          {PHASES.map((p) => (
            <PhaseBadge key={p} phase={p} />
          ))}
          <Badge tone="tomato">Rejected</Badge>
          <MockBadge />
          <MockBadge inline />
        </div>
      </Section>

      <Section title="Balls">
        <div className="flex flex-wrap items-center gap-4">
          <Ball n={7} color="tomato" />
          <Ball n={42} color="sun" />
          <Ball n={13} color="cobalt" />
          <Ball n={88} color="mint" />
          <Ball n={500} color="paper" />
        </div>
        <BallCluster />
      </Section>

      <Section title="Tickets">
        <div className="grid gap-8 md:grid-cols-2">
          <Ticket
            tone="mint"
            stub={
              <>
                <PhaseBadge phase="OPEN" />
                <Button size="sm">Enter</Button>
              </>
            }
          >
            <p className="font-mono text-xs font-semibold uppercase tracking-widest text-ink-3">Fair draw</p>
            <h3 className="mt-1 font-display text-2xl">Moonlight Rooftop Sessions</h3>
            <p className="mt-2 text-ink-2">One night, 500 seats, a skyline and a very good sound system.</p>
          </Ticket>
          <Ticket
            tone="sun"
            stub={
              <>
                <span className="tnum font-display text-2xl font-extrabold">09:42</span>
                <Button size="sm" variant="primary">
                  Claim
                </Button>
              </>
            }
          >
            <h3 className="font-display text-2xl">You’re in!</h3>
            <p className="mt-2 text-ink-2">A seat is being held for you. Claim it before the timer runs out.</p>
          </Ticket>
          <Ticket>
            <h3 className="font-display text-2xl">No stub</h3>
            <p className="mt-2 text-ink-2">Same outline and shadow, plain card.</p>
          </Ticket>
        </div>
      </Section>

      <Section title="Alerts">
        <div className="grid gap-4 md:grid-cols-2">
          <Alert tone="info" title="The window hasn’t opened yet">
            No advantage to being early: everyone who enters during the window has the same chance.
          </Alert>
          <Alert tone="warn" title="Let’s slow down a little" action={<Button size="sm" variant="secondary" disabled>Try again in 6s</Button>}>
            We are getting a lot of requests from your connection.
          </Alert>
          <Alert tone="error" title="Something went wrong on our side">
            Nothing you did caused this. Please try again in a moment.
          </Alert>
          <Alert tone="success" title="You’re entered">
            Your entry is recorded. Nothing more to do until the draw.
          </Alert>
          <Alert tone="offline" title="Reconnecting…">
            We lost the live connection and are checking in at a relaxed pace.
          </Alert>
        </div>
      </Section>

      <Section title="Countdown and challenges">
        <Countdown target={new Date(Date.now() + 2 * 86_400_000 + 3 * 3_600_000 + 125_000).toISOString()} label="Entries open in" />
        <div className="grid gap-4 md:grid-cols-2">
          <ChallengePanel state={{ kind: 'pow', difficultyBits: 18, hashes: 120_000 }} onCaptcha={() => undefined} onCancel={() => undefined} />
          <ChallengePanel
            state={{
              kind: 'captcha',
              challenge: { id: 'c', type: 'captcha', expires_at: '2030-01-01T00:00:00.000Z', captcha: { provider: 'mock', site_key: 'k' } },
            }}
            onCaptcha={() => undefined}
          />
        </div>
      </Section>

      <Section title="Form">
        <div className="grid max-w-xl gap-5">
          <Field label="Email" type="email" placeholder="you@example.com" hint="We’ll send a one-time code." />
          <Field label="Display name" defaultValue="Asha" />
          <Field label="One-time code" defaultValue="12" error="That code is not right" inputMode="numeric" />
        </div>
      </Section>

      <Section title="Loading">
        <div className="flex items-center gap-6">
          <Spinner label="Loading" />
          <Skeleton className="h-8 w-48" />
        </div>
      </Section>
    </div>
  );
}
