import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeft } from 'lucide-react';
import { describeError } from '@/api/errors';
import { Alert } from '@/ui/Alert';
import { Badge } from '@/ui/Badge';
import { Button, ButtonLink } from '@/ui/Button';
import { Skeleton } from '@/ui/Skeleton';
import { AdminShell } from '@/features/admin/AdminShell';
import { ResultsView } from './ResultsView';
import { simApi, simKeys } from './simApi';
import { fmt } from './stats';
import { useRunProgress, type RunProgress } from './useRunProgress';

function Progress({ runId, p }: { runId: string; p: RunProgress }) {
  const [cancelling, setCancelling] = useState(false);
  const run = p.run;
  const last = p.snapshots[p.snapshots.length - 1];
  const live = run && (run.status === 'queued' || run.status === 'running');

  return (
    <section aria-label="Run progress" className="space-y-4 rounded-lg border-2 border-ink bg-paper-2 p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Badge tone={run?.status === 'running' ? 'sun' : run?.status === 'done' ? 'mint' : run?.status === 'failed' ? 'tomato' : 'neutral'} dot={run?.status === 'running'}>
            {run?.status ?? 'connecting'}
          </Badge>
          <span className="text-xs text-ink-3">{p.via === 'sse' ? 'live' : p.via === 'poll' ? 'checking every few seconds' : ''}</span>
        </div>
        {live && (
          <Button
            variant="secondary"
            size="sm"
            loading={cancelling}
            onClick={async () => {
              setCancelling(true);
              await simApi.cancel(runId).catch(() => undefined);
              setCancelling(false);
            }}
          >
            Cancel run
          </Button>
        )}
      </div>
      <div
        role="progressbar"
        aria-label="Run progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round((run?.progress ?? 0) * 100)}
        className="h-4 overflow-hidden rounded-full border-2 border-ink bg-paper"
      >
        <div className="h-full bg-cobalt transition-[width] duration-300 motion-reduce:transition-none" style={{ width: `${Math.max(2, (run?.progress ?? 0) * 100)}%` }} />
      </div>
      <p className="tnum text-sm text-ink-2" aria-live="polite">
        {Math.round((run?.progress ?? 0) * 100)}% · {run?.phase_text ?? 'Waiting for the simulator…'}
      </p>
      {last && live && (
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {[
            ['Requests so far', last.requests !== undefined ? fmt(last.requests, 'count') : '–'],
            ['Throughput', last.throughput_rps !== undefined ? fmt(last.throughput_rps, 'rps') : '–'],
            ['Entry p95', last.p95_ms !== undefined ? fmt(last.p95_ms, 'ms') : '–'],
            ['Bot seat share (running)', last.bot_seat_share !== undefined ? fmt(last.bot_seat_share, 'pct') : '–'],
          ].map(([k, v]) => (
            <div key={k} className="rounded-md border-2 border-ink bg-paper px-3 py-2">
              <dt className="text-xs text-ink-3">{k}</dt>
              <dd className="tnum text-lg font-semibold">{v}</dd>
            </div>
          ))}
        </dl>
      )}
      {live && <p className="text-xs text-ink-3">Live numbers are provisional snapshots; final numbers come with confidence intervals when the run finishes.</p>}
      {p.error && !run && (
        <Alert tone="warn" title="Can’t reach the simulator">
          {p.error}
        </Alert>
      )}
    </section>
  );
}

export function Component() {
  const { id = '' } = useParams();
  const run = useQuery({ queryKey: simKeys.run(id), queryFn: ({ signal }) => simApi.run(id, signal), enabled: !!id });
  const progress = useRunProgress(id);
  const status = progress.run?.status ?? run.data?.status;
  const results = useQuery({ queryKey: simKeys.results(id), queryFn: ({ signal }) => simApi.results(id, signal), enabled: status === 'done', staleTime: Infinity });
  const scenarios = useQuery({ queryKey: simKeys.scenarios, queryFn: ({ signal }) => simApi.scenarios(signal), staleTime: 5 * 60_000 });
  const scenarioName = scenarios.data?.find((s) => s.id === (results.data?.scenario_id ?? run.data?.scenario_id))?.name;

  return (
    <AdminShell title={`Run ${id}`}>
      <Link to="/admin/sim" className="inline-flex items-center gap-1 text-sm font-semibold text-ink-2 hover:text-ink">
        <ArrowLeft className="size-4" aria-hidden="true" /> Simulator
      </Link>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="font-display text-3xl">
          {scenarioName ?? 'Run'} <span className="font-mono text-lg text-ink-3">{id}</span>
        </h1>
        {status === 'done' && (
          <ButtonLink to={`/admin/sim/compare?b=${encodeURIComponent(id)}`} variant="secondary" size="sm">
            Compare with another run
          </ButtonLink>
        )}
      </div>

      {run.error && (
        <Alert tone={describeError(run.error).tone} title={describeError(run.error).title}>
          {describeError(run.error).body}
        </Alert>
      )}
      {status !== 'done' && !run.error && <Progress runId={id} p={progress} />}
      {status === 'failed' && <Alert tone="error" title="The run failed">The simulator reported a failure. Its logs will say why.</Alert>}
      {status === 'cancelled' && <Alert title="Cancelled">This run was stopped before it finished, so there are no results.</Alert>}
      {status === 'done' && results.isPending && <Skeleton className="h-96" />}
      {results.error && (
        <Alert tone={describeError(results.error).tone} title="Results couldn’t be loaded">
          {describeError(results.error).body}
        </Alert>
      )}
      {results.data && <ResultsView r={results.data} scenarioName={scenarioName} />}
    </AdminShell>
  );
}
