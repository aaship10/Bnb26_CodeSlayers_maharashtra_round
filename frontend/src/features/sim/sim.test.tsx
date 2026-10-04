// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { coerceAndValidate, initialValues, overridesFrom, parseParamsSchema } from './jsonSchemaForm';
import { ChartTable, isShareChart, toRows, wantsLogX } from './ChartView';
import { ResultsView } from './ResultsView';
import { attackMismatch, Component as ComparePage } from './ComparePage';
import { Component as SimPanel } from './SimPanel';
import { ciText, fmt } from './stats';
import { simApi } from './simApi';
import type { ChartData, JsonSchema, Scenario, SimResults, Stat } from './schemas';
import { ApiError } from '@/api/errors';
import { adminToken } from '@/features/admin/adminToken';
import { renderRoute } from '@/test/render';

const schema: JsonSchema = {
  type: 'object',
  properties: {
    bots: { type: 'integer', title: 'Bot identities', minimum: 0, maximum: 100, default: 20 },
    rate: { type: 'number', title: 'Rate', minimum: 1, maximum: 1000, default: 100 },
    mode: { type: 'string', title: 'Mode', enum: ['LOTTERY', 'FCFS'], default: 'LOTTERY' },
    broken: { type: 'boolean', title: 'Broken', default: false },
    steps: { type: 'array', items: { type: 'number', minimum: 1 }, default: [1, 10] },
    weird: { type: 'object', properties: {} },
  },
  required: ['bots', 'mode'],
};

describe('JSON Schema parameter form', () => {
  const { fields, unsupported } = parseParamsSchema(schema);

  it('builds one field per supported property, and reports the rest instead of dropping them', () => {
    expect(fields.map((f) => [f.key, f.kind])).toEqual([
      ['bots', 'integer'],
      ['rate', 'number'],
      ['mode', 'enum'],
      ['broken', 'boolean'],
      ['steps', 'number-list'],
    ]);
    expect(unsupported).toEqual(['weird (object)']);
    expect(fields.find((f) => f.key === 'bots')).toMatchObject({ required: true, min: 0, max: 100, label: 'Bot identities' });
  });

  it('starts from the defaults', () => {
    expect(initialValues(fields)).toEqual({ bots: '20', rate: '100', mode: 'LOTTERY', broken: false, steps: '1, 10' });
  });

  it('coerces types and enforces the schema limits with readable messages', () => {
    const bad = coerceAndValidate(fields, { bots: '2.5', rate: '5000', mode: 'X', broken: true, steps: '1, x' });
    expect(bad.errors).toEqual({ bots: 'Enter a whole number.', rate: 'At most 1000.', mode: 'Pick one of the options.', steps: 'Enter numbers separated by commas.' });
    const empty = coerceAndValidate(fields, { bots: '', rate: '', mode: '', broken: false, steps: '' });
    expect(empty.errors.bots).toBe('Required.');
    expect(empty.errors.rate).toBeUndefined(); // optional
    const ok = coerceAndValidate(fields, { bots: '7', rate: '2.5', mode: 'FCFS', broken: true, steps: '1 3 30' });
    expect(ok.errors).toEqual({});
    expect(ok.values).toEqual({ bots: 7, rate: 2.5, mode: 'FCFS', broken: true, steps: [1, 3, 30] });
  });

  it('sends only what differs from the defaults as overrides', () => {
    const { values } = coerceAndValidate(fields, { bots: '20', rate: '300', mode: 'LOTTERY', broken: false, steps: '1, 10' });
    expect(overridesFrom(fields, values)).toEqual({ rate: 300 });
  });
});

const chart = (over: Partial<ChartData> = {}): ChartData => ({
  chart_id: 'c',
  title: 'Bot share',
  x_label: 'Request rate',
  y_label: 'Share of seats',
  series: [
    { name: 'Fair Drop', points: [{ x: 1, y: 0.004, ci_low: 0.003, ci_high: 0.005 }, { x: 1000, y: 0.004, ci_low: 0.003, ci_high: 0.006 }] },
    { name: 'FCFS', points: [{ x: 1, y: 0.2, ci_low: 0.18, ci_high: 0.22 }, { x: 1000, y: 0.97, ci_low: 0.96, ci_high: 0.98 }] },
  ],
  target: 'mock',
  synthetic: true,
  ...over,
});

describe('chart helpers', () => {
  it('pivots series into rows with CI bands and error offsets', () => {
    const rows = toRows(chart());
    expect(rows).toHaveLength(2);
    expect(rows[1]).toMatchObject({ x: 1000, 'Fair Drop': 0.004, FCFS: 0.97, 'FCFS__band': [0.96, 0.98] });
    const err = rows[1]!['FCFS__err'] as [number, number];
    expect(err[0]).toBeCloseTo(0.01, 9);
    expect(err[1]).toBeCloseTo(0.01, 9);
  });

  it('uses a log axis for data spanning orders of magnitude, percentages for shares, plain numbers for latency', () => {
    expect(wantsLogX(chart())).toBe(true);
    expect(isShareChart(chart())).toBe(true);
    expect(isShareChart(chart({ y_label: 'Latency (ms)', series: [{ name: 'a', points: [{ x: 'p50', y: 40 }] }] }))).toBe(false);
    expect(wantsLogX(chart({ series: [{ name: 'a', points: [{ x: 'p50', y: 0.1 }] }] }))).toBe(false);
  });

  it('the table view carries every value with its CI', () => {
    render(<ChartTable c={chart()} />);
    expect(screen.getByRole('columnheader', { name: 'FCFS' })).toBeInTheDocument();
    expect(screen.getByText('97%')).toBeInTheDocument();
    expect(screen.getByText('95% CI 96%–98%')).toBeInTheDocument();
  });
});

const st = (mean: number, half = 0.01, n = 10): Stat => ({ mean, ci_low: mean - half, ci_high: mean + half, n });

export function results(over: { mode?: 'LOTTERY' | 'FCFS'; share?: number; passed?: boolean; run?: string } = {}): SimResults {
  const share = over.share ?? 0.004;
  return {
    schema_version: 1,
    run_id: over.run ?? 'run_0001',
    scenario_id: 'bot_swarm',
    target: 'mock',
    synthetic: true,
    seed: 42,
    repeats: 10,
    population: { legit: 50_000, bots: 200, bot_identities: 200 },
    event: { inventory: 500, mode: over.mode ?? 'LOTTERY', defences: { preset: 'rate_limit+pow' } },
    metrics: {
      fairness: {
        bot_seat_share: st(share),
        bot_entrant_share: st(0.004),
        human_win_prob: st(0.00996, 0.0004),
        human_entry_success_rate: st(0.996),
        arrival_time_correlation: st(0.001),
        jain_index: st(0.995),
        gini: st(0.01),
        attacker_cost_per_seat: { requests: st(800_000, 20_000), accounts: st(100, 3), pow_hashes: null },
      },
      system: {
        latency_ms: { enter: { p50: 40, p95: 120, p99: 240 }, status: { p50: 12, p95: 36, p99: 72 }, claim: { p50: 45, p95: 135, p99: 270 } },
        throughput_rps: st(4200, 100),
        error_rates: { http_429_legit: st(0.004, 0.001), http_429_bot: st(0.91), http_5xx: 0, timeout: st(0, 0) },
      },
      detection: { precision: st(0.97), recall: st(0.18), false_positive_rate: st(0.003, 0.001) },
      integrity: { oversold: over.passed === false ? 3 : 0, duplicate_users: 0, duplicate_seats: 0, orphaned_holds: 0, draw_verified: true, passed: over.passed ?? true },
    },
  };
}

describe('formatting', () => {
  it('a mean is always printed with its CI and n', () => {
    expect(fmt(0.1234, 'pct')).toBe('12.3%');
    expect(fmt(0.004, 'pct')).toBe('0.40%');
    expect(ciText(st(0.5, 0.05, 8), 'pct')).toBe('95% CI 45.0%–55.0% · n = 8');
  });
});

describe('ResultsView', () => {
  it('shows every fairness number with its CI and n, never a bare mean', () => {
    render(<ResultsView r={results()} />);
    const fairness = screen.getByRole('region', { name: 'Fairness' });
    const values = within(fairness).getAllByText(/^\d|^-/, { selector: 'p.font-semibold' });
    expect(values.length).toBe(7);
    for (const v of values) expect(v.nextElementSibling?.textContent).toMatch(/95% CI .* · n = 10/);
  });

  it('labels plain single values as having no CI, and missing ones as not applicable', () => {
    render(<ResultsView r={results()} />);
    const system = screen.getByRole('region', { name: 'System' });
    expect(within(system).getByText('single value, no CI reported')).toBeInTheDocument(); // the 5xx rate
    expect(within(screen.getByRole('region', { name: 'Attacker cost per seat won' })).getByText('Not applicable')).toBeInTheDocument();
  });

  it('wears the synthetic banner and shows integrity, red when it failed', () => {
    const { unmount } = render(<ResultsView r={results()} />);
    expect(screen.getByTestId('synthetic-banner')).toHaveTextContent('Mock / synthetic data');
    expect(screen.getByTestId('integrity')).toHaveAttribute('data-passed', 'true');
    unmount();
    render(<ResultsView r={results({ passed: false })} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Integrity FAILED');
    expect(screen.getByRole('alert')).toHaveTextContent('oversold 3');
  });
});

describe('comparison', () => {
  it('flags comparisons between runs that did not face the same attack', () => {
    const a = results({ mode: 'LOTTERY' });
    const b = results({ mode: 'FCFS' });
    expect(attackMismatch(a, b)).toEqual([]);
    expect(attackMismatch(a, { ...b, population: { ...b.population, bot_identities: 999 } })).toContain('different numbers of bot identities');
    expect(attackMismatch(a, results({ mode: 'LOTTERY' }))).toContain('both runs use LOTTERY');
  });

  afterEach(() => {
    vi.restoreAllMocks();
    adminToken.clear();
  });

  it('puts the two runs side by side on shared axes with CIs and differences', async () => {
    adminToken.set('t');
    vi.spyOn(simApi, 'runs').mockResolvedValue([
      { run_id: 'run_0002', scenario_id: 'bot_swarm', status: 'done', progress: 1, params: { mode: 'FCFS' } },
      { run_id: 'run_0001', scenario_id: 'bot_swarm', status: 'done', progress: 1, params: { mode: 'LOTTERY' } },
    ]);
    vi.spyOn(simApi, 'results').mockImplementation(async (id) => (id === 'run_0001' ? results({ mode: 'LOTTERY', share: 0.004 }) : results({ mode: 'FCFS', share: 0.97, run: 'run_0002' })));
    renderRoute(<ComparePage />, { path: '/admin/sim/compare', route: '/admin/sim/compare' });
    const row = await screen.findByTestId('cmp-Share of seats won by bots');
    expect(row).toHaveTextContent('0.40%');
    expect(row).toHaveTextContent('97.0%');
    expect(row).toHaveTextContent('−96.6 pp');
    expect(row).toHaveTextContent('Fair Drop better');
    expect(within(row).getAllByText(/95% CI /)).toHaveLength(2);
    expect(within(row).getAllByText('n = 10')).toHaveLength(2);
    expect(within(row).getByRole('img', { name: /Fair Drop 0.40%.*FCFS 97.0%/ })).toBeInTheDocument();
    expect(screen.getByTestId('synthetic-banner')).toBeInTheDocument();
  });
});

describe('simulator panel', () => {
  const scenario: Scenario = {
    id: 'bot_swarm',
    name: 'Fast bots',
    description: 'd',
    profile: 'attack',
    estimated_duration_s: 10,
    scale: '50,000 logical users',
    params_schema: {
      type: 'object',
      properties: {
        bots: { type: 'integer', title: 'Bot identities', minimum: 0, maximum: 10000, default: 200 },
        request_multiplier: { type: 'number', title: 'Bot request rate', minimum: 1, maximum: 1000, default: 100 },
        mode: { type: 'string', title: 'Allocation mode', enum: ['LOTTERY', 'FCFS'], default: 'LOTTERY' },
        defence_preset: { type: 'string', title: 'Defence preset', enum: ['none', 'rate_limit+pow'], default: 'rate_limit+pow' },
      },
      required: ['bots'],
    },
  };

  beforeEach(() => {
    adminToken.set('t');
    vi.spyOn(simApi, 'scenarios').mockResolvedValue([scenario]);
    vi.spyOn(simApi, 'runs').mockResolvedValue([]);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    adminToken.clear();
  });

  it('generates the form from the schema, validates it, and starts a run with only the changed values', async () => {
    const start = vi.spyOn(simApi, 'start').mockResolvedValue({ run_id: 'run_0009' });
    renderRoute(<SimPanel />, { path: '/admin/sim', route: '/admin/sim' });
    const user = userEvent.setup();
    const bots = await screen.findByLabelText('Bot identities');
    expect(bots).toHaveValue(200);
    await user.clear(bots);
    await user.type(bots, '20000');
    await user.click(screen.getByRole('button', { name: 'Start run' }));
    expect(screen.getByText('At most 10000.')).toBeInTheDocument();
    expect(start).not.toHaveBeenCalled();

    await user.clear(bots);
    await user.type(bots, '500');
    await user.click(screen.getByRole('button', { name: 'Start run' }));
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    expect(start.mock.calls[0]![0]).toEqual({ scenario_id: 'bot_swarm', overrides: { bots: 500 }, repeats: 5, seed: 42, target: 'mock' });
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/admin/sim/runs/run_0009'));
  });

  it('demo presets prefill the form (FCFS under attack)', async () => {
    renderRoute(<SimPanel />, { path: '/admin/sim', route: '/admin/sim' });
    await screen.findByLabelText('Bot identities');
    await userEvent.click(screen.getByRole('button', { name: '1 · FCFS under attack' }));
    expect(screen.getByLabelText('Allocation mode')).toHaveValue('FCFS');
    expect(screen.getByLabelText('Defence preset')).toHaveValue('none');
  });

  it('target switch is explicit, and a refusal from the mock is shown verbatim', async () => {
    vi.spyOn(simApi, 'start').mockRejectedValue(new ApiError('VALIDATION_ERROR', 'This is the mock simulator: it can only run target "mock".', 409));
    renderRoute(<SimPanel />, { path: '/admin/sim', route: '/admin/sim' });
    await screen.findByLabelText('Bot identities');
    await userEvent.click(screen.getByRole('radio', { name: 'Real stack' }));
    expect(screen.getByRole('radio', { name: 'Real stack' })).toHaveAttribute('aria-checked', 'true');
    await userEvent.click(screen.getByRole('button', { name: 'Start run' }));
    expect(await screen.findByText(/can only run target "mock"/)).toBeInTheDocument();
  });
});
