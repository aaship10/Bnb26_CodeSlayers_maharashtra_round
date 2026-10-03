import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeft, ImageIcon } from 'lucide-react';
import { describeError } from '@/api/errors';
import { cx } from '@/lib/cx';
import { Alert } from '@/ui/Alert';
import { MockBadge } from '@/ui/Badge';
import { Skeleton } from '@/ui/Skeleton';
import { AdminShell } from '@/features/admin/AdminShell';
import { ChartView } from './ChartView';
import { simApi, simKeys } from './simApi';
import { SyntheticBanner } from './stats';
import type { Experiment } from './schemas';

/** One chart: the live dataset when it loads and validates, the static PNG otherwise. */
function ChartCard({ chartId, exp }: { chartId: string; exp: Experiment }) {
  const q = useQuery({ queryKey: simKeys.chart(chartId, exp.id), queryFn: ({ signal }) => simApi.chart(chartId, exp.id, signal), staleTime: 5 * 60_000, retry: 1 });
  const png = simApi.chartPngUrl(chartId, exp.id);
  return (
    <section className="rounded-lg border-2 border-ink bg-paper-2 p-4" data-testid={`chart-${chartId}`}>
      {(q.data?.synthetic || q.data?.target === 'mock') && <MockBadge inline className="mb-2" />}
      {q.isPending && <Skeleton className="h-80" />}
      {q.data && <ChartView c={q.data} />}
      {q.error && (
        <div className="space-y-2">
          <Alert tone="warn" title="Showing the static image">
            The chart data couldn’t be loaded ({describeError(q.error).title}), so here is the simulator’s rendered PNG instead.
          </Alert>
          <img src={png} alt={`Chart ${chartId} (static image)`} className="w-full rounded-md border-2 border-ink bg-white" loading="lazy" />
        </div>
      )}
      {q.data && (
        <a href={png} target="_blank" rel="noreferrer" className="mt-2 inline-flex items-center gap-1 text-xs text-ink-3 hover:text-ink">
          <ImageIcon className="size-3.5" aria-hidden="true" /> Static image
        </a>
      )}
    </section>
  );
}

export function Component() {
  const exps = useQuery({ queryKey: simKeys.experiments, queryFn: ({ signal }) => simApi.experiments(signal), staleTime: 60_000 });
  const [selected, setSelected] = useState<string | null>(null);
  const list = exps.data ?? [];
  const current = list.find((e) => e.id === selected) ?? list[0];

  return (
    <AdminShell title="Experiments">
      <Link to="/admin/sim" className="inline-flex items-center gap-1 text-sm font-semibold text-ink-2 hover:text-ink">
        <ArrowLeft className="size-4" aria-hidden="true" /> Simulator
      </Link>
      <div>
        <h1 className="font-display text-3xl">Experiments</h1>
        <p className="text-ink-2">The evidence: how allocation moves under different attacks, with 95% confidence bands.</p>
      </div>

      {exps.isPending && <Skeleton className="h-64" />}
      {exps.error && (
        <Alert tone={describeError(exps.error).tone} title="Can’t load experiments">
          {describeError(exps.error).body}
        </Alert>
      )}
      {exps.data?.length === 0 && <Alert title="No experiments yet">They appear here once the simulator has published some.</Alert>}

      {current && (
        <>
          <div role="tablist" aria-label="Experiments" className="flex flex-wrap gap-2">
            {list.map((e) => (
              <button
                key={e.id}
                type="button"
                role="tab"
                aria-selected={current.id === e.id}
                onClick={() => setSelected(e.id)}
                className={cx('rounded-full border-2 border-ink px-3 py-1 text-sm font-semibold', current.id === e.id ? 'bg-ink text-paper' : 'bg-paper-2 hover:bg-sun-tint')}
              >
                {e.title}
              </button>
            ))}
          </div>
          <div role="tabpanel" aria-label={current.title} className="space-y-4">
            <SyntheticBanner target={current.target} synthetic={current.synthetic} />
            <p className="max-w-prose text-ink-2">{current.description}</p>
            <div className="grid gap-5 xl:grid-cols-2">
              {current.charts.map((id) => (
                <ChartCard key={`${current.id}:${id}`} chartId={id} exp={current} />
              ))}
            </div>
          </div>
        </>
      )}
    </AdminShell>
  );
}
