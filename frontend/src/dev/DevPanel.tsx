import { useCallback, useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Clock, FlaskConical, Plug, RotateCcw, X } from 'lucide-react';
import { mockApi, type MockState } from './mockApi';
import { cx } from '@/lib/cx';

const ADVANCES: [string, number][] = [
  ['+10s', 10_000],
  ['+1m', 60_000],
  ['+10m', 600_000],
  ['+1h', 3_600_000],
];

const SPEEDS = [0, 1, 10, 60];

function fmt(iso: string): string {
  return iso.replace('T', ' ').replace(/\.\d+Z$/, 'Z');
}

const chip =
  'rounded-sm border border-ink bg-paper-2 px-2 py-1 text-left font-mono text-[11px] leading-tight hover:bg-sun-tint disabled:opacity-50';

/**
 * Floating controls for the mock server: scenario switcher, time travel,
 * failure injection. Dev builds only (main.tsx drops it from production).
 */
export default function DevPanel() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<MockState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // difference between the mock server's clock and this tab's, to tick the display between polls
  const [skew, setSkew] = useState(0);
  const [, force] = useState(0);

  const apply = useCallback(
    (s: MockState) => {
      setState(s);
      setSkew(Date.parse(s.server_now) - Date.now());
      setError(null);
    },
    [setState],
  );

  const refresh = useCallback(() => mockApi.state().then(apply).catch((e: unknown) => setError(String(e))), [apply]);

  const run = useCallback(
    async (fn: () => Promise<MockState | unknown>) => {
      setBusy(true);
      try {
        const out = await fn();
        if (out && typeof out === 'object' && 'scenario' in out) apply(out as MockState);
        else await refresh();
        await qc.invalidateQueries();
      } catch (e) {
        setError(String(e));
      } finally {
        setBusy(false);
      }
    },
    [apply, refresh, qc],
  );

  useEffect(() => {
    if (!open) return;
    void refresh();
    const poll = setInterval(() => void refresh(), 4000);
    const tick = setInterval(() => force((n) => n + 1), 1000);
    return () => {
      clearInterval(poll);
      clearInterval(tick);
    };
  }, [open, refresh]);

  const toggleFault = (key: string) => {
    if (!state) return;
    const next = state.faults.includes(key) ? state.faults.filter((f) => f !== key) : [...state.faults, key];
    void run(() => mockApi.faults(next));
  };

  const groups = state ? (['Timeline', 'Outcomes', 'Failures'] as const) : [];
  const mockNow = state ? new Date(Date.now() + skew).toISOString() : '';

  return (
    <div className="fixed bottom-3 right-3 z-50 font-mono text-xs text-ink">
      {!open && (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="flex items-center gap-1.5 rounded-full border-2 border-ink bg-paper-2 px-3 py-2 font-bold shadow-pop-sm hover:bg-sun-tint"
          aria-label="Open mock server controls"
        >
          <FlaskConical className="size-4" aria-hidden="true" /> Mock
        </button>
      )}

      {open && (
        <section
          aria-label="Mock server controls"
          className="max-h-[82dvh] w-[min(23rem,calc(100vw-1.5rem))] overflow-y-auto rounded-lg border-2 border-ink bg-paper-3 shadow-pop"
        >
          <header className="sticky top-0 z-10 flex items-center justify-between border-b-2 border-ink bg-ink px-3 py-2 text-paper">
            <span className="flex items-center gap-2 font-bold">
              <Plug className="size-4" aria-hidden="true" />
              Mock server
              <span className={cx('size-2 rounded-full', error ? 'bg-tomato' : 'bg-mint')} title={error ?? 'connected'} />
            </span>
            <button type="button" onClick={() => setOpen(false)} aria-label="Close mock controls" className="rounded p-1 hover:bg-ink-2">
              <X className="size-4" aria-hidden="true" />
            </button>
          </header>

          {error && !state && (
            <p className="p-3 text-tomato-deep">
              Can’t reach the mock server. Start it with <b>npm run mock</b>.
            </p>
          )}

          {state && (
            <div className="space-y-4 p-3">
              <div>
                <h3 className="mb-1 flex items-center gap-1.5 font-bold uppercase tracking-wider text-ink-3">
                  <Clock className="size-3.5" aria-hidden="true" /> Server clock
                </h3>
                <p className="tnum text-sm font-bold" data-testid="mock-now">
                  {fmt(mockNow)}
                </p>
                <p className="text-ink-3">
                  window {fmt(state.timeline.opens).slice(11, 16)} to {fmt(state.timeline.closes).slice(11, 16)}, scenario <b>{state.scenario}</b>
                </p>
                <div className="mt-2 grid grid-cols-2 gap-1">
                  {state.time_presets.map((p) => (
                    <button key={p.id} type="button" className={chip} disabled={busy} onClick={() => void run(() => mockApi.clockSet(p.iso))}>
                      {p.label}
                    </button>
                  ))}
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-1">
                  {ADVANCES.map(([label, ms]) => (
                    <button key={label} type="button" className={chip} disabled={busy} onClick={() => void run(() => mockApi.clockAdvance(ms))}>
                      {label}
                    </button>
                  ))}
                  <label className="ml-auto flex items-center gap-1">
                    speed
                    <select
                      value={state.speed}
                      onChange={(e) => void run(() => mockApi.clockSpeed(Number(e.target.value)))}
                      className="rounded-sm border border-ink bg-paper-2 px-1 py-0.5"
                    >
                      {SPEEDS.map((s) => (
                        <option key={s} value={s}>
                          {s === 0 ? 'paused' : `${s}x`}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                {state.speed > 1 && (
                  <p className="mt-1 text-ink-3">At {state.speed}x, on-page countdowns drift until the next response re-syncs them.</p>
                )}
              </div>

              <div>
                <h3 className="mb-1 font-bold uppercase tracking-wider text-ink-3">Scenario</h3>
                {groups.map((g) => (
                  <fieldset key={g} className="mb-2">
                    <legend className="mb-1 text-ink-3">{g}</legend>
                    <div className="grid gap-1">
                      {state.scenarios
                        .filter((s) => s.group === g)
                        .map((s) => (
                          <button
                            key={s.id}
                            type="button"
                            disabled={busy}
                            title={s.description}
                            aria-pressed={state.scenario === s.id}
                            onClick={() => void run(() => mockApi.scenario(s.id))}
                            className={cx(chip, state.scenario === s.id && 'bg-sun font-bold')}
                          >
                            {s.name}
                          </button>
                        ))}
                    </div>
                  </fieldset>
                ))}
              </div>

              <div>
                <h3 className="mb-1 font-bold uppercase tracking-wider text-ink-3">Inject failures</h3>
                <table className="w-full border-collapse text-left">
                  <thead>
                    <tr className="text-ink-3">
                      <th className="py-1 font-normal">kind</th>
                      {state.fault_targets.map((t) => (
                        <th key={t} className="px-1 py-1 text-center font-normal">
                          {t}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {state.fault_kinds.map((k) => (
                      <tr key={k} className="border-t border-rule">
                        <td className="py-1">{k}</td>
                        {state.fault_targets.map((t) => {
                          const key = `${t}:${k}`;
                          return (
                            <td key={t} className="text-center">
                              <input
                                type="checkbox"
                                aria-label={`${k} on ${t}`}
                                checked={state.faults.includes(key)}
                                disabled={busy}
                                onChange={() => toggleFault(key)}
                              />
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="mt-1 text-ink-3">
                  429 lifts after ~8 s, challenges clear once solved, network_fail and server_error self-clear after 3 hits.
                </p>
              </div>

              <label className="flex items-center gap-2">
                <input type="checkbox" checked={state.sse_enabled} disabled={busy} onChange={(e) => void run(() => mockApi.setSse(e.target.checked))} />
                Live stream (SSE) enabled <span className="text-ink-3">(off forces polling)</span>
              </label>

              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={state.invariants_broken}
                  disabled={busy}
                  onChange={(e) => void run(() => mockApi.breakInvariants(e.target.checked))}
                />
                Break invariants <span className="text-ink-3">(demo the red badge)</span>
              </label>

              <div className="flex flex-wrap gap-1">
                <button type="button" className={chip} disabled={busy} onClick={() => void run(() => mockApi.dropSse())}>
                  Drop SSE streams ({state.sse_clients})
                </button>
                <button type="button" className={cx(chip, 'flex items-center gap-1')} disabled={busy} onClick={() => void run(mockApi.reset)}>
                  <RotateCcw className="size-3" aria-hidden="true" /> Reset scenario
                </button>
              </div>

              <p className="border-t border-rule pt-2 text-ink-3">
                Sign-up code for any email: <b className="text-ink">{state.otp}</b>
                <br />
                Mock CAPTCHA token: <b className="text-ink">{state.captcha_token}</b>
                <br />
                Admin token: <b className="text-ink">{state.admin_token}</b> (organizer pages at /admin)
              </p>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
