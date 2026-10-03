import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Dice5, Play } from 'lucide-react';
import { describeError } from '@/api/errors';
import { cx } from '@/lib/cx';
import { Alert } from '@/ui/Alert';
import { Badge } from '@/ui/Badge';
import { Button, ButtonLink } from '@/ui/Button';
import { Skeleton } from '@/ui/Skeleton';
import { AdminShell } from '@/features/admin/AdminShell';
import { coerceAndValidate, initialValues, overridesFrom, parseParamsSchema } from './jsonSchemaForm';
import { ParamsForm } from './ParamsForm';
import { simApi, simKeys } from './simApi';
import type { RunSummary, Scenario } from './schemas';

/** One-click setups that mirror the demo script (section 13). They only prefill the form. */
export const DEMO_PRESETS: { label: string; scenario: string; values: Record<string, string | boolean> }[] = [
  { label: '1 · FCFS under attack', scenario: 'bot_swarm', values: { mode: 'FCFS', defence_preset: 'none', request_multiplier: '100' } },
  { label: '2 · Fair Drop, same attack', scenario: 'bot_swarm', values: { mode: 'LOTTERY', defence_preset: 'none', request_multiplier: '100' } },
  { label: '3 · Bots 100× faster', scenario: 'bot_swarm', values: { mode: 'LOTTERY', defence_preset: 'none', request_multiplier: '1000' } },
  { label: '4 · 10,000 sybils', scenario: 'sybil_farm', values: { mode: 'LOTTERY', bot_identities: '10000' } },
  { label: '5 · Kill a replica', scenario: 'replica_kill', values: { mode: 'LOTTERY' } },
];

const STATUS_TONE: Record<string, 'neutral' | 'sun' | 'mint' | 'tomato' | 'cobalt'> = {
  queued: 'neutral',
  running: 'sun',
  done: 'mint',
  failed: 'tomato',
  cancelled: 'neutral',
};

function RecentRuns() {
  const runs = useQuery({ queryKey: simKeys.runs, queryFn: ({ signal }) => simApi.runs(signal), refetchInterval: 10_000 });
  if (runs.isPending) return <Skeleton className="h-24" />;
  if (runs.error) return <p className="text-sm text-ink-3">The simulator didn’t return a run list ({describeError(runs.error).title}).</p>;
  if (runs.data.length === 0) return <p className="text-sm text-ink-3">No runs yet.</p>;
  const label = (r: RunSummary) => [r.params?.mode, r.params?.defence_preset].filter(Boolean).join(' · ');
  return (
    <ul className="divide-y-2 divide-rule overflow-hidden rounded-lg border-2 border-ink bg-paper-2">
      {runs.data.slice(0, 12).map((r) => (
        <li key={r.run_id}>
          <Link to={`/admin/sim/runs/${encodeURIComponent(r.run_id)}`} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2.5 hover:bg-sun-tint">
            <span className="font-mono text-sm">{r.run_id}</span>
            <span className="min-w-0 flex-1 truncate font-semibold">{r.scenario_name ?? r.scenario_id}</span>
            <span className="text-sm text-ink-3">{label(r)}</span>
            <Badge tone={STATUS_TONE[r.status]}>{r.status}</Badge>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function RunForm({ scenario, preset }: { scenario: Scenario; preset: Record<string, string | boolean> | null }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const parsed = useMemo(() => parseParamsSchema(scenario.params_schema), [scenario]);
  const [values, setValues] = useState(() => ({ ...initialValues(parsed.fields), ...(preset ?? {}) }));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [repeats, setRepeats] = useState('5');
  const [seed, setSeed] = useState('42');
  const [target, setTarget] = useState<'mock' | 'real'>('mock');
  const [pending, setPending] = useState(false);
  const [serverError, setServerError] = useState<unknown>(null);

  useEffect(() => {
    setValues({ ...initialValues(parsed.fields), ...(preset ?? {}) });
    setErrors({});
    setServerError(null);
  }, [parsed, preset]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const { values: typed, errors: errs } = coerceAndValidate(parsed.fields, values);
    const rep = Number(repeats);
    const sd = Number(seed);
    if (!Number.isInteger(rep) || rep < 1 || rep > 50) errs.__repeats = 'Repeats must be 1 to 50.';
    if (!Number.isInteger(sd)) errs.__seed = 'Seed must be a whole number.';
    setErrors(errs);
    if (Object.keys(errs).length) return;
    setPending(true);
    setServerError(null);
    try {
      const { run_id } = await simApi.start({ scenario_id: scenario.id, overrides: overridesFrom(parsed.fields, typed), repeats: rep, seed: sd, target });
      void queryClient.invalidateQueries({ queryKey: simKeys.runs });
      navigate(`/admin/sim/runs/${encodeURIComponent(run_id)}`);
    } catch (err) {
      setServerError(err);
    } finally {
      setPending(false);
    }
  };

  const se = serverError ? describeError(serverError) : null;

  return (
    <form onSubmit={submit} noValidate className="space-y-5" aria-label={`Configure ${scenario.name}`}>
      {parsed.unsupported.length > 0 && (
        <Alert tone="warn" title="Some parameters can’t be edited here">
          {parsed.unsupported.join(', ')} use schema features this form doesn’t support; they keep their defaults.
        </Alert>
      )}
      <ParamsForm fields={parsed.fields} values={values} errors={errors} onChange={(k, v) => setValues((s) => ({ ...s, [k]: v }))} />

      <div className="grid gap-4 border-t-2 border-dashed border-rule pt-4 sm:grid-cols-3">
        <div>
          <label htmlFor="repeats" className="mb-1 block font-display text-sm font-bold">
            Repeats
          </label>
          <input id="repeats" type="number" min={1} max={50} value={repeats} onChange={(e) => setRepeats(e.target.value)} className="min-h-11 w-full rounded-md border-2 border-ink bg-paper-2 px-3" aria-describedby="repeats-d" />
          <p id="repeats-d" className="mt-1 text-xs text-ink-3">
            More repeats, tighter confidence intervals.
          </p>
          {errors.__repeats && <p className="text-sm font-semibold text-tomato-deep">{errors.__repeats}</p>}
        </div>
        <div>
          <label htmlFor="seed" className="mb-1 block font-display text-sm font-bold">
            Seed
          </label>
          <div className="flex gap-2">
            <input id="seed" type="number" value={seed} onChange={(e) => setSeed(e.target.value)} className="min-h-11 w-full rounded-md border-2 border-ink bg-paper-2 px-3" />
            <Button type="button" variant="secondary" size="sm" aria-label="Random seed" onClick={() => setSeed(String(Math.floor(Math.random() * 1e6)))}>
              <Dice5 className="size-5" aria-hidden="true" />
            </Button>
          </div>
          {errors.__seed && <p className="text-sm font-semibold text-tomato-deep">{errors.__seed}</p>}
        </div>
        <fieldset>
          <legend className="mb-1 font-display text-sm font-bold">Target</legend>
          <div role="radiogroup" className="flex overflow-hidden rounded-md border-2 border-ink">
            {(['mock', 'real'] as const).map((t) => (
              <button
                key={t}
                type="button"
                role="radio"
                aria-checked={target === t}
                onClick={() => setTarget(t)}
                className={cx('min-h-11 flex-1 px-3 text-sm font-semibold', target === t ? (t === 'mock' ? 'bg-tomato-tint text-tomato-deep' : 'bg-ink text-paper') : 'bg-paper-2')}
              >
                {t === 'mock' ? 'Mock (synthetic)' : 'Real stack'}
              </button>
            ))}
          </div>
          <p className="mt-1 text-xs text-ink-3">{target === 'mock' ? 'Fast, synthetic numbers. Always badged as such.' : 'Load against the running backend. Takes longer.'}</p>
        </fieldset>
      </div>

      {se && (
        <Alert tone={se.tone} title={se.title}>
          {se.detail ?? se.body}
        </Alert>
      )}
      <Button type="submit" size="lg" loading={pending} leading={<Play className="size-5" aria-hidden="true" />}>
        Start run
      </Button>
    </form>
  );
}

export function Component() {
  const scenarios = useQuery({ queryKey: simKeys.scenarios, queryFn: ({ signal }) => simApi.scenarios(signal), staleTime: 5 * 60_000 });
  const [selected, setSelected] = useState<string | null>(null);
  const [preset, setPreset] = useState<{ scenario: string; values: Record<string, string | boolean>; n: number } | null>(null);
  const list = scenarios.data ?? [];
  const current = list.find((s) => s.id === selected) ?? list[0];

  const applyPreset = (p: (typeof DEMO_PRESETS)[number]) => {
    setSelected(p.scenario);
    setPreset((prev) => ({ scenario: p.scenario, values: p.values, n: (prev?.n ?? 0) + 1 }));
  };
  const presetFor = current && preset?.scenario === current.id ? preset.values : null;

  return (
    <AdminShell title="Simulator">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-3xl">Simulator</h1>
          <p className="text-ink-2">Run the same crowd and the same attack against either allocation mode, then compare.</p>
        </div>
        <div className="flex gap-2">
          <ButtonLink to="/admin/sim/compare" variant="secondary" size="sm">
            FCFS vs Fair Drop
          </ButtonLink>
          <ButtonLink to="/admin/sim/experiments" variant="secondary" size="sm">
            Experiments
          </ButtonLink>
        </div>
      </div>

      <div>
        <p className="mb-2 font-display text-sm font-bold uppercase tracking-widest text-ink-3">Demo presets</p>
        <div className="flex flex-wrap gap-2">
          {DEMO_PRESETS.map((p) => (
            <button key={p.label} type="button" onClick={() => applyPreset(p)} className="rounded-full border-2 border-ink bg-paper-2 px-3 py-1 text-sm font-semibold hover:bg-sun-tint">
              {p.label}
            </button>
          ))}
        </div>
      </div>

      {scenarios.isPending && <Skeleton className="h-72" />}
      {scenarios.error && (
        <Alert tone={describeError(scenarios.error).tone} title="Can’t reach the simulator">
          {describeError(scenarios.error).body}
        </Alert>
      )}

      {current && (
        <div className="grid gap-6 lg:grid-cols-[18rem_1fr]">
          <div role="radiogroup" aria-label="Scenario" className="space-y-2">
            {list.map((s) => (
              <button
                key={s.id}
                type="button"
                role="radio"
                aria-checked={current.id === s.id}
                onClick={() => setSelected(s.id)}
                className={cx('block w-full rounded-md border-2 border-ink p-3 text-left', current.id === s.id ? 'bg-sun-tint shadow-pop-sm' : 'bg-paper-2 hover:bg-paper-3')}
              >
                <span className="flex items-center justify-between gap-2">
                  <span className="font-semibold">{s.name}</span>
                  <Badge tone={s.profile === 'attack' ? 'tomato' : s.profile === 'chaos' ? 'sun' : 'neutral'}>{s.profile}</Badge>
                </span>
                <span className="mt-1 block text-sm text-ink-2">{s.description}</span>
                <span className="mt-1 block text-xs text-ink-3">
                  ~{s.estimated_duration_s} s{typeof s.scale === 'string' ? ` · ${s.scale}` : ''}
                </span>
              </button>
            ))}
          </div>
          <section className="rounded-lg border-2 border-ink bg-paper-2 p-4 sm:p-5" aria-labelledby="cfg-h">
            <h2 id="cfg-h" className="mb-4 font-display text-xl">
              {current.name}
            </h2>
            <RunForm key={`${current.id}:${preset?.n ?? 0}`} scenario={current} preset={presetFor} />
          </section>
        </div>
      )}

      <section aria-labelledby="runs-h" className="space-y-3">
        <h2 id="runs-h" className="font-display text-xl">
          Recent runs
        </h2>
        <RecentRuns />
      </section>
    </AdminShell>
  );
}
