import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { CircleCheck, CircleDashed, CircleMinus, CircleX, ShieldCheck } from 'lucide-react';
import { api, apiClient, describeError } from '@/api';
import { pollInterval } from '@/api/polling';
import { queryKeys } from '@/api/queryClient';
import { cx } from '@/lib/cx';
import { formatCount } from '@/lib/format';
import { useDocumentTitle } from '@/lib/useDocumentTitle';
import { useSession } from '@/state/session';
import { Alert } from '@/ui/Alert';
import { MockBadge, PhaseBadge } from '@/ui/Badge';
import { Ball } from '@/ui/Ball';
import { Button } from '@/ui/Button';
import { Field } from '@/ui/Field';
import { Skeleton } from '@/ui/Skeleton';
import { Spinner } from '@/ui/Spinner';
import { Ticket } from '@/ui/Ticket';
import { PROVISIONAL_ALGORITHM } from './drawSpec';
import { HashValue } from './HashValue';
import { fairnessSchema, type Fairness } from './schemas';
import { useDrawVerifier } from './useDrawVerifier';
import type { Step } from './verifyDraw';

const fairnessPoll = pollInterval(30_000, 15_000);

export function useFairness(id: string) {
  return useQuery({
    queryKey: ['events', id, 'fairness'],
    queryFn: ({ signal }) => apiClient.request(`/events/${encodeURIComponent(id)}/fairness`, { schema: fairnessSchema, auth: false, signal }),
    enabled: !!id,
    // Values appear in stages (close, draw). Check back occasionally until the result is out.
    refetchInterval: (q) => (q.state.data?.result ? false : fairnessPoll(q)),
  });
}

function Row({ label, when, children }: { label: string; when: string; children: ReactNode }) {
  return (
    <div className="grid gap-1 border-t border-rule py-3 first:border-t-0 sm:grid-cols-[13rem_1fr] sm:gap-4">
      <dt>
        <span className="block font-semibold">{label}</span>
        <span className="block text-xs text-ink-3">{when}</span>
      </dt>
      <dd className="min-w-0">{children}</dd>
    </div>
  );
}

const Pending = ({ children }: { children: ReactNode }) => <span className="text-sm italic text-ink-3">{children}</span>;

function PublicRecord({ f, eventId }: { f: Fairness; eventId: string }) {
  return (
    <Ticket stub={<span className="text-sm text-ink-2">Anyone can copy these values and check them independently.</span>}>
      <h2 className="font-display text-2xl">The public record</h2>
      <dl className="mt-3">
        <Row label="Seed commitment" when="Published before the window opened">
          <HashValue value={f.seed_commitment} label="seed commitment" />
        </Row>
        <Row label="Server seed" when="Revealed after the draw">
          {f.server_seed ? <HashValue value={f.server_seed} label="server seed" /> : <Pending>Kept secret until the draw, so nobody (us included) can predict it.</Pending>}
        </Row>
        <Row label="Public beacon" when="Randomness nobody controls">
          {f.beacon ? (
            <div className="space-y-1">
              <p className="text-sm">
                {f.beacon.source}, round <b className="tnum">{f.beacon.round}</b>
              </p>
              {f.beacon.randomness ? <HashValue value={f.beacon.randomness} label="beacon randomness" /> : <Pending>Not published yet.</Pending>}
            </div>
          ) : (
            <Pending>Announced before the window.</Pending>
          )}
        </Row>
        <Row label="Entrant list" when="Locked in when the window closed">
          {f.entrants_hash ? (
            <div className="space-y-1">
              <p className="text-sm">
                <b className="tnum">{formatCount(f.entrants_count ?? 0)}</b> entries, hashing to
              </p>
              <HashValue value={f.entrants_hash} label="entrants hash" />
            </div>
          ) : (
            <Pending>Published when the window closes.</Pending>
          )}
        </Row>
        <Row label="Final seed" when="Seed + beacon + entrant list">
          {f.final_seed ? <HashValue value={f.final_seed} label="final seed" /> : <Pending>Computed at the draw.</Pending>}
        </Row>
        <Row label="Result" when="Winners first, then the waitlist">
          {f.result ? (
            <p className="text-sm">
              <b className="tnum">{formatCount(f.result.winners_count)}</b> winners and <b className="tnum">{formatCount(f.result.waitlist_count)}</b> waitlisted.
            </p>
          ) : (
            <Pending>After the draw.</Pending>
          )}
        </Row>
        <Row label="Algorithm" when="How the ranking is computed">
          <p className="font-mono text-sm">{f.algorithm_version}</p>
          {f.algorithm_version === PROVISIONAL_ALGORITHM && (
            <p className="mt-1 text-xs text-ink-3">Byte encodings follow our provisional spec until the backend publishes its final one.</p>
          )}
        </Row>
        <Row label="Audit log head" when="Last link of the hash chain">
          {f.audit_head_hash ? (
            <div className="space-y-1">
              <HashValue value={f.audit_head_hash} label="audit head hash" />
              <Link to={`/events/${encodeURIComponent(eventId)}/audit`} className="link text-sm font-semibold">
                Browse and check the audit log
              </Link>
            </div>
          ) : (
            <Pending>No entries yet.</Pending>
          )}
        </Row>
      </dl>
    </Ticket>
  );
}

const STEP_ICON: Record<Step['status'], ReactNode> = {
  pending: <CircleDashed className="size-5 text-ink-3" aria-hidden="true" />,
  running: <Spinner className="size-5" />,
  pass: <CircleCheck className="size-5 text-pine" aria-hidden="true" />,
  fail: <CircleX className="size-5 text-tomato-deep" aria-hidden="true" />,
  skipped: <CircleMinus className="size-5 text-ink-3" aria-hidden="true" />,
};

const STEP_WORD: Record<Step['status'], string> = { pending: 'Waiting', running: 'Running', pass: 'Passed', fail: 'Failed', skipped: 'Skipped' };

function StepList({ steps, progress }: { steps: Step[]; progress: number }) {
  return (
    <ol className="space-y-3" aria-label="Verification steps">
      {steps.map((s, i) => (
        <li key={s.id} className="flex gap-3" data-testid={`step-${s.id}`} data-status={s.status}>
          <span className="mt-0.5 shrink-0">{STEP_ICON[s.status]}</span>
          <div className="min-w-0 flex-1">
            <p className="font-semibold">
              {i + 1}. {s.title} <span className="sr-only">({STEP_WORD[s.status]})</span>
              {s.ms !== undefined && s.status !== 'pending' && <span className="ml-2 font-mono text-xs font-normal text-ink-3">{s.ms} ms</span>}
            </p>
            {s.detail && <p className={cx('mt-0.5 break-words text-sm', s.status === 'fail' ? 'font-semibold text-tomato-deep' : 'text-ink-2')}>{s.detail}</p>}
            {s.id === 'rank' && s.status === 'running' && (
              <div
                className="mt-2 h-2.5 max-w-sm overflow-hidden rounded-full border-2 border-ink bg-paper-2"
                role="progressbar"
                aria-label="Ranking entries"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(progress * 100)}
              >
                <div className="h-full bg-cobalt" style={{ width: `${Math.max(3, progress * 100)}%` }} />
              </div>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}

function FindYourself({ order, winnersCount, initial, synthetic }: { order: string[] | null; winnersCount: number | null; initial: string; synthetic: boolean }) {
  const [id, setId] = useState(initial);
  useEffect(() => setId(initial), [initial]);
  const index = useMemo(() => (order && id.trim() ? order.indexOf(id.trim()) : -2), [order, id]);

  let result: ReactNode = null;
  if (order && id.trim()) {
    if (index === -1) {
      result = (
        <Alert title="That draw ID isn’t in the entrant list">
          Check for typos. {synthetic ? 'The demo list is synthetic, so test accounts aren’t in it; try one of the IDs from the list instead.' : 'Only people who entered before the window closed are in the draw.'}
        </Alert>
      );
    } else if (winnersCount !== null && index < winnersCount) {
      result = (
        <Alert tone="success" title={`Picked: winner #${formatCount(index + 1)} of ${formatCount(winnersCount)}`}>
          Recomputed by your browser, not taken from the server.
        </Alert>
      );
    } else if (winnersCount !== null) {
      result = (
        <Alert title={`Waitlist position ${formatCount(index - winnersCount + 1)}`}>
          {formatCount(index - winnersCount + 1)} place{index - winnersCount === 0 ? '' : 's'} behind the last winner. Recomputed by your browser.
        </Alert>
      );
    }
  }

  return (
    <div className="space-y-3">
      <Field label="Your draw ID" value={id} onChange={(e) => setId(e.target.value)} placeholder="p_…" autoComplete="off" spellCheck={false} hint="It’s on your status page after the draw." />
      {!order && <p className="text-sm text-ink-3">Run the check above to find your place.</p>}
      {order && order.length > 0 && !id.trim() && (
        <button type="button" className="link text-sm font-semibold" onClick={() => setId(order[0]!)}>
          Try the first winner’s ID
        </button>
      )}
      {result}
    </div>
  );
}

export function Component() {
  const { id = '' } = useParams();
  const location = useLocation();
  const session = useSession();
  const statePublicId = (location.state as { publicId?: unknown } | null)?.publicId;
  const fairness = useFairness(id);
  const event = useQuery({ queryKey: queryKeys.event(id), queryFn: ({ signal }) => api.events.get(id, signal), enabled: !!id, staleTime: 60_000 });
  // Auto-fill "your draw ID" from your own status when signed in (A exposes public_id after the draw).
  const status = useQuery({
    queryKey: queryKeys.status(id),
    queryFn: ({ signal }) => api.events.status(id, signal),
    enabled: !!session && !!id && typeof statePublicId !== 'string',
    staleTime: 60_000,
  });
  const verifier = useDrawVerifier(id);
  useDocumentTitle('How this draw stays fair');

  const myId = typeof statePublicId === 'string' ? statePublicId : (status.data?.public_id ?? '');
  const f = fairness.data;

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <header className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          {f && <PhaseBadge phase={f.phase} />}
          {f?.synthetic && <MockBadge inline />}
        </div>
        <h1 className="font-display text-3xl sm:text-5xl">How this draw stays fair</h1>
        {event.data && (
          <p className="text-ink-2">
            for{' '}
            <Link to={`/events/${encodeURIComponent(id)}`} className="link font-semibold">
              {event.data.name}
            </Link>
          </p>
        )}
      </header>

      <ol className="grid gap-5 sm:grid-cols-3">
        {[
          { n: 1, c: 'sun' as const, t: 'We commit first', b: 'Before the window opens we publish a fingerprint of a secret seed. We can’t change the seed later without the fingerprint giving it away.' },
          { n: 2, c: 'cobalt' as const, t: 'Then mix in chance', b: 'At the draw we reveal the seed and combine it with a public random beacon and the locked entrant list. Nobody knew the beacon in advance.' },
          { n: 3, c: 'mint' as const, t: 'Anyone can replay it', b: 'The ranking is pure maths on public values. Your browser can redo the whole draw and check it gets the same winners.' },
        ].map((s) => (
          <li key={s.n} className="flex gap-3 sm:flex-col">
            <Ball n={s.n} color={s.c} className="size-12 shrink-0" />
            <div>
              <h2 className="font-display text-lg">{s.t}</h2>
              <p className="mt-1 text-sm text-ink-2">{s.b}</p>
            </div>
          </li>
        ))}
      </ol>

      {fairness.isPending && <Skeleton className="h-96" />}
      {fairness.error && (
        <Alert tone={describeError(fairness.error).tone} title={describeError(fairness.error).title}>
          {describeError(fairness.error).body}
        </Alert>
      )}
      {f && <PublicRecord f={f} eventId={id} />}

      {f && (
        <section aria-labelledby="verify-h" className="space-y-5 rounded-lg border-2 border-ink bg-paper-2 p-5 sm:p-6">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <h2 id="verify-h" className="flex items-center gap-2 font-display text-2xl">
                <ShieldCheck className="size-6" aria-hidden="true" /> Check it yourself
              </h2>
              <p className="mt-1 max-w-prose text-sm text-ink-2">
                Runs entirely in your browser: it downloads the entrant list{f.entrants_count ? ` (${formatCount(f.entrants_count)} entries)` : ''} and recomputes
                the draw from the public values above. Nothing is taken on trust.
              </p>
            </div>
            {verifier.running ? (
              <Button variant="secondary" onClick={verifier.cancel}>
                Stop
              </Button>
            ) : (
              <Button onClick={() => verifier.run(f)}>{verifier.verdict ? 'Run it again' : 'Verify the draw'}</Button>
            )}
          </div>

          {verifier.steps.length > 0 && <StepList steps={verifier.steps} progress={verifier.progress} />}

          <div aria-live="polite">
            {verifier.verdict === 'verified' && (
              <Alert tone="success" title="Verified: this draw is exactly reproducible">
                Every check passed in {((verifier.ms ?? 0) / 1000).toFixed(1)} s on your device. The winners and the waitlist are exactly what these public values produce.
              </Alert>
            )}
            {verifier.verdict === 'failed' && (
              <Alert tone="error" title="This draw does not match its public record">
                Step {verifier.steps.findIndex((s) => s.status === 'fail') + 1} failed; the reason is shown above. Please report this to the organisers.
              </Alert>
            )}
            {verifier.verdict === 'not_yet' && (
              <Alert title="Not everything can be checked yet">The remaining steps unlock as the window closes and the draw runs. What’s published so far checked out.</Alert>
            )}
            {verifier.error && (
              <Alert tone="error" title="The check couldn’t finish">
                {verifier.error}
              </Alert>
            )}
          </div>

          <div className="border-t-2 border-dashed border-rule pt-5">
            <h3 className="mb-3 font-display text-lg">Find yourself in the draw</h3>
            <FindYourself order={verifier.order} winnersCount={verifier.winnersCount} initial={myId} synthetic={!!f.synthetic} />
          </div>
        </section>
      )}
    </div>
  );
}
