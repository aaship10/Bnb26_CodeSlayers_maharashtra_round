import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import type { ServerResponse } from 'node:http';
import { createHash } from 'node:crypto';
import { sendError } from './faults';
import { renderChartPng } from './png';

/**
 * Stand-in for Member C's simulator service (/sim). Everything it produces is
 * SYNTHETIC: numbers come from a small model with seeded noise, chosen to show
 * the behaviour the real experiments are expected to measure. Every payload says
 * target "mock" and synthetic true, and the UI badges it.
 *
 * Model in one paragraph: under FCFS, seats go to whoever is fastest, so bot
 * share climbs with request rate. Under the lottery each identity has one equal
 * entry, so request rate is irrelevant and bot share is ~ effective bot
 * identities / all identities, where defences reduce how many bot identities get
 * a valid entry.
 */

type Mode = 'LOTTERY' | 'FCFS';
const PRESETS = ['none', 'rate_limit', 'rate_limit+pow', 'rate_limit+pow+captcha', 'all'] as const;
type Preset = (typeof PRESETS)[number];

/* ---------------------------------------------------------------- scenarios */

const legit = { type: 'integer', title: 'Real people', description: 'Logical users simulated as async clients.', minimum: 100, maximum: 50_000, default: 50_000 };
const inventory = { type: 'integer', title: 'Seats', minimum: 1, maximum: 5_000, default: 500 };
const mode = { type: 'string', title: 'Allocation mode', enum: ['LOTTERY', 'FCFS'], default: 'LOTTERY' };
const preset = { type: 'string', title: 'Defence preset', enum: [...PRESETS], default: 'rate_limit+pow' };

export const SIM_SCENARIOS = [
  {
    id: 'flash_crowd',
    name: 'Flash crowd, no attack',
    description: 'Everyone arrives in the first minutes. The baseline for latency and fairness without bots.',
    profile: 'baseline',
    estimated_duration_s: 8,
    scale: '50,000 logical users',
    params_schema: {
      type: 'object',
      properties: { legit_users: legit, inventory, mode },
      required: ['legit_users', 'inventory', 'mode'],
    },
  },
  {
    id: 'bot_swarm',
    name: 'Fast bots',
    description: 'A few hundred bot identities hammering the API far faster than a person can.',
    profile: 'attack',
    estimated_duration_s: 10,
    scale: '50,000 logical users + bots',
    params_schema: {
      type: 'object',
      properties: {
        legit_users: legit,
        bots: { type: 'integer', title: 'Bot identities', minimum: 0, maximum: 10_000, default: 200 },
        request_multiplier: { type: 'number', title: 'Bot request rate (× a person)', minimum: 1, maximum: 1000, default: 100 },
        inventory,
        mode,
        defence_preset: preset,
      },
      required: ['legit_users', 'bots', 'request_multiplier', 'inventory', 'mode', 'defence_preset'],
    },
  },
  {
    id: 'sybil_farm',
    name: 'Sybil farm',
    description: 'One attacker controlling many verified-looking accounts, each entering once.',
    profile: 'attack',
    estimated_duration_s: 10,
    scale: '50,000 logical users + sybils',
    params_schema: {
      type: 'object',
      properties: {
        legit_users: legit,
        bot_identities: { type: 'integer', title: 'Sybil identities', minimum: 1, maximum: 20_000, default: 1000 },
        inventory,
        mode,
        defence_preset: preset,
      },
      required: ['legit_users', 'bot_identities', 'inventory', 'mode', 'defence_preset'],
    },
  },
  {
    id: 'replica_kill',
    name: 'Kill a replica mid-run',
    description: 'One API replica is killed during the entry window. State must survive.',
    profile: 'chaos',
    estimated_duration_s: 12,
    scale: '50,000 logical users',
    params_schema: {
      type: 'object',
      properties: {
        legit_users: legit,
        bots: { type: 'integer', title: 'Bot identities', minimum: 0, maximum: 10_000, default: 200 },
        kill_at_s: { type: 'number', title: 'Kill a replica after (s)', minimum: 1, maximum: 60, default: 10 },
        inventory,
        mode,
        simulate_broken_build: {
          type: 'boolean',
          title: 'Simulate a broken build',
          description: 'Demo only: makes the integrity checks fail so the red banner can be seen.',
          default: false,
        },
      },
      required: ['legit_users', 'kill_at_s', 'inventory', 'mode'],
    },
  },
] as const;

/* -------------------------------------------------------------- the model */

function rng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Standard normal via Box-Muller. */
function gauss(r: () => number): number {
  const u = Math.max(1e-12, r());
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * r());
}

const T975 = [NaN, 12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.16, 2.145, 2.131, 2.12, 2.11, 2.101, 2.093, 2.086];

export interface Stat {
  mean: number;
  ci_low: number;
  ci_high: number;
  n: number;
}

/** Mean and 95% t-interval of R noisy draws around `truth`. */
function stat(truth: number, sd: number, repeats: number, r: () => number, clamp?: [number, number]): Stat {
  const xs = Array.from({ length: repeats }, () => {
    const v = truth + gauss(r) * sd;
    return clamp ? Math.min(clamp[1], Math.max(clamp[0], v)) : v;
  });
  const mean = xs.reduce((a, b) => a + b, 0) / repeats;
  const s = repeats > 1 ? Math.sqrt(xs.reduce((a, b) => a + (b - mean) ** 2, 0) / (repeats - 1)) : 0;
  const t = T975[Math.min(repeats - 1, 20)] ?? 1.96;
  const half = repeats > 1 ? (t * s) / Math.sqrt(repeats) : 0;
  let lo = mean - half;
  let hi = mean + half;
  if (clamp) {
    lo = Math.max(clamp[0], lo);
    hi = Math.min(clamp[1], hi);
  }
  const round = (x: number) => Math.round(x * 1e6) / 1e6;
  return { mean: round(mean), ci_low: round(lo), ci_high: round(hi), n: repeats };
}

/** Fraction of bot identities that still get a valid entry past the defences. */
function passRate(p: Preset): number {
  return { none: 1, rate_limit: 0.97, 'rate_limit+pow': 0.82, 'rate_limit+pow+captcha': 0.45, all: 0.28 }[p];
}

/** How much of a bot's speed advantage survives (FCFS only). */
function speedSurvives(p: Preset): number {
  return { none: 1, rate_limit: 0.25, 'rate_limit+pow': 0.12, 'rate_limit+pow+captcha': 0.08, all: 0.05 }[p];
}

export function botSeatShare(m: Mode, legitUsers: number, botIds: number, multiplier: number, p: Preset, seats: number): number {
  if (botIds === 0) return 0;
  const effIds = botIds * passRate(p);
  if (m === 'LOTTERY') return effIds / (effIds + legitUsers);
  const pressure = (effIds * multiplier * speedSurvives(p)) / (seats * 0.5);
  return Math.min(0.985, 1 - Math.exp(-pressure));
}

interface RunInput {
  scenario_id: string;
  params: Record<string, unknown>;
  repeats: number;
  seed: number;
}

function num(v: unknown, d: number) {
  return typeof v === 'number' && Number.isFinite(v) ? v : d;
}

export function computeResults(runId: string, input: RunInput) {
  const p = input.params;
  const r = rng(input.seed ^ parseInt(createHash('sha256').update(JSON.stringify([input.scenario_id, p])).digest('hex').slice(0, 8), 16));
  const R = input.repeats;
  const L = num(p.legit_users, 50_000);
  const N = num(p.inventory, 500);
  const m = (p.mode === 'FCFS' ? 'FCFS' : 'LOTTERY') as Mode;
  const pr = ((PRESETS as readonly string[]).includes(String(p.defence_preset)) ? p.defence_preset : input.scenario_id === 'flash_crowd' ? 'none' : 'rate_limit+pow') as Preset;
  const B = input.scenario_id === 'sybil_farm' ? num(p.bot_identities, 1000) : input.scenario_id === 'flash_crowd' ? 0 : num(p.bots, 200);
  const M = input.scenario_id === 'bot_swarm' ? num(p.request_multiplier, 100) : input.scenario_id === 'sybil_farm' ? 1 : 20;
  const attack = B > 0;
  const lottery = m === 'LOTTERY';

  const share = botSeatShare(m, L, B, M, pr, N);
  const effIds = B * passRate(pr);
  const entrantShare = lottery ? effIds / (effIds + L) : Math.min(0.95, share * 0.6);
  const humanSeats = N * (1 - share);
  const unit: [number, number] = [0, 1];

  const overload = !lottery && attack ? Math.min(1, (B * M) / 20_000) : 0;
  const lat = (base: number) => ({
    p50: Math.round(base * (1 + overload * 6)),
    p95: Math.round(base * 3 * (1 + overload * 12)),
    p99: Math.round(base * 6 * (1 + overload * 18) * (input.scenario_id === 'replica_kill' ? 2.2 : 1)),
  });
  const broken = input.scenario_id === 'replica_kill' && p.simulate_broken_build === true;
  const totalBotRequests = B * M * 40;

  return {
    schema_version: 1,
    run_id: runId,
    scenario_id: input.scenario_id,
    target: 'mock',
    synthetic: true,
    seed: input.seed,
    repeats: R,
    population: { legit: L, bots: B, bot_identities: B },
    event: { inventory: N, mode: m, defences: { preset: pr } },
    metrics: {
      fairness: {
        bot_seat_share: stat(share, Math.max(0.0015, share * 0.06), R, r, unit),
        bot_entrant_share: stat(entrantShare, Math.max(0.001, entrantShare * 0.05), R, r, unit),
        human_win_prob: stat(humanSeats / L, (humanSeats / L) * 0.04, R, r, unit),
        human_entry_success_rate: stat(lottery ? 0.996 : attack ? 0.42 : 0.81, 0.01, R, r, unit),
        arrival_time_correlation: stat(lottery ? 0 : 0.84, 0.02, R, r, [-1, 1]),
        jain_index: stat(lottery ? 0.995 : 0.31, 0.01, R, r, unit),
        gini: stat(lottery ? 0.01 : 0.68, 0.01, R, r, unit),
        attacker_cost_per_seat: attack
          ? {
              requests: stat(totalBotRequests / Math.max(1, share * N), totalBotRequests / Math.max(1, share * N) * 0.05, R, r),
              accounts: stat(B / Math.max(1, share * N), (B / Math.max(1, share * N)) * 0.05, R, r),
              pow_hashes: pr.includes('pow') || pr === 'all' ? stat((totalBotRequests * 2 ** 18) / Math.max(1, share * N), 1e8, R, r) : null,
            }
          : { requests: null, accounts: null, pow_hashes: null },
      },
      system: {
        latency_ms: { enter: lat(38), status: lat(12), claim: lat(45) },
        throughput_rps: stat(lottery ? 4200 : 3100, 120, R, r),
        error_rates: {
          http_429_legit: stat(pr === 'none' ? 0 : 0.004, 0.001, R, r, unit),
          http_429_bot: stat(attack && pr !== 'none' ? 0.91 : 0, 0.02, R, r, unit),
          http_5xx: stat(input.scenario_id === 'replica_kill' ? 0.008 : overload * 0.05, 0.001, R, r, unit),
          timeout: stat(overload * 0.08, 0.002, R, r, unit),
        },
      },
      detection:
        attack && pr !== 'none'
          ? {
              precision: stat(0.97 - (pr === 'rate_limit' ? 0.2 : 0), 0.01, R, r, unit),
              recall: stat(1 - passRate(pr), 0.02, R, r, unit),
              false_positive_rate: stat(0.003, 0.001, R, r, unit),
            }
          : { precision: null, recall: null, false_positive_rate: null },
      integrity: {
        oversold: broken ? 3 : 0,
        duplicate_users: 0,
        duplicate_seats: broken ? 1 : 0,
        orphaned_holds: 0,
        draw_verified: lottery ? !broken : null,
        passed: !broken,
      },
    },
  };
}

/* ------------------------------------------------------------ experiments */

type Point = { x: number | string; y: number; ci_low: number; ci_high: number };

function series(name: string, xs: (number | string)[], f: (x: number | string, i: number) => number, rel: number, seed: number, clamp = true) {
  const r = rng(seed);
  return {
    name,
    points: xs.map((x, i): Point => {
      const y = f(x, i);
      const half = Math.max(0.0008, Math.abs(y) * rel * (0.6 + r() * 0.8));
      const lo = clamp ? Math.max(0, y - half) : y - half;
      const hi = clamp ? Math.min(1, y + half) : y + half;
      return { x, y: Math.round(y * 1e5) / 1e5, ci_low: Math.round(lo * 1e5) / 1e5, ci_high: Math.round(hi * 1e5) / 1e5 };
    }),
  };
}

const L0 = 50_000;
const N0 = 500;

export const CHARTS: Record<string, { title: string; x_label: string; y_label: string; notes?: string; series: ReturnType<typeof series>[] }> = {
  bot_share_vs_request_multiplier: {
    title: 'Bot seat share as bots send more requests',
    x_label: 'Bot request rate (× a person, log scale)',
    y_label: 'Share of seats won by bots',
    notes: '200 bot identities, 50,000 people, 500 seats, rate limit + PoW. 10 repeats per point.',
    series: [
      series('Fair Drop (lottery)', [1, 3, 10, 30, 100, 300, 1000], (x) => botSeatShare('LOTTERY', L0, 200, Number(x), 'rate_limit+pow', N0), 0.12, 11),
      series('First come, first served', [1, 3, 10, 30, 100, 300, 1000], (x) => botSeatShare('FCFS', L0, 200, Number(x), 'none', N0), 0.04, 12),
    ],
  },
  bot_share_vs_identities: {
    title: 'Bot seat share as an attacker adds identities',
    x_label: 'Sybil identities (log scale)',
    y_label: 'Share of seats won by bots',
    notes: 'Lottery mode. Defences bend the curve by making each identity costlier to get a valid entry.',
    series: [
      series('No defences', [10, 100, 1000, 5000, 20000], (x) => botSeatShare('LOTTERY', L0, Number(x), 1, 'none', N0), 0.08, 21),
      series('Rate limit + PoW', [10, 100, 1000, 5000, 20000], (x) => botSeatShare('LOTTERY', L0, Number(x), 1, 'rate_limit+pow', N0), 0.08, 22),
      series('All layers', [10, 100, 1000, 5000, 20000], (x) => botSeatShare('LOTTERY', L0, Number(x), 1, 'all', N0), 0.08, 23),
    ],
  },
  human_win_prob_under_attack: {
    title: 'A real person’s chance of a seat under attack',
    x_label: 'Bot identities (log scale)',
    y_label: 'Probability a person wins a seat',
    series: [
      series('Fair Drop (lottery)', [10, 100, 1000, 5000, 20000], (x) => (N0 * (1 - botSeatShare('LOTTERY', L0, Number(x), 100, 'rate_limit+pow', N0))) / L0, 0.05, 31),
      series('First come, first served', [10, 100, 1000, 5000, 20000], (x) => (N0 * (1 - botSeatShare('FCFS', L0, Number(x), 100, 'none', N0))) / L0, 0.08, 32),
    ],
  },
  latency_percentiles_normal_vs_attack: {
    title: 'Entry latency, normal crowd vs under attack',
    x_label: 'Percentile',
    y_label: 'Latency (ms)',
    series: [
      series('Normal crowd', ['p50', 'p95', 'p99'], (_x, i) => [38, 115, 230][i]!, 0.08, 41, false),
      series('Attack, Fair Drop', ['p50', 'p95', 'p99'], (_x, i) => [44, 160, 340][i]!, 0.08, 42, false),
      series('Attack, FCFS', ['p50', 'p95', 'p99'], (_x, i) => [260, 1900, 4100][i]!, 0.1, 43, false),
    ],
  },
  detection_precision_recall_by_layer: {
    title: 'Detection quality by defence layer',
    x_label: 'Layer',
    y_label: 'Score',
    series: [
      series('Precision', ['Rate limit', 'PoW', 'CAPTCHA', 'Signals', 'Risk'], (_x, i) => [0.78, 0.9, 0.97, 0.88, 0.95][i]!, 0.03, 51),
      series('Recall', ['Rate limit', 'PoW', 'CAPTCHA', 'Signals', 'Risk'], (_x, i) => [0.35, 0.18, 0.55, 0.42, 0.61][i]!, 0.06, 52),
    ],
  },
  ablation_bot_share: {
    title: 'Which defence does the work (lottery, 5,000 sybils)',
    x_label: 'Defence preset',
    y_label: 'Share of seats won by bots',
    series: [
      series(
        'Bot seat share',
        ['None', 'Rate limit', '+ PoW', '+ CAPTCHA', 'All'],
        (_x, i) => botSeatShare('LOTTERY', L0, 5000, 1, PRESETS[i]!, N0),
        0.07,
        61,
      ),
    ],
  },
};

export const EXPERIMENTS = [
  {
    id: 'exp_request_rate',
    title: 'Speed doesn’t buy seats',
    description: 'Same bots, same people; only the bots’ request rate changes. FCFS rewards speed; the lottery ignores it.',
    charts: ['bot_share_vs_request_multiplier', 'latency_percentiles_normal_vs_attack'],
  },
  {
    id: 'exp_sybil',
    title: 'Identities are the real limiter',
    description: 'What an attacker gets by adding accounts, and how defences bend the curve.',
    charts: ['bot_share_vs_identities', 'human_win_prob_under_attack'],
  },
  {
    id: 'exp_ablation',
    title: 'Defence ablation',
    description: 'Turn layers on one at a time and see which ones move the numbers.',
    charts: ['ablation_bot_share', 'detection_precision_recall_by_layer'],
  },
].map((e, i) => ({ ...e, run_ids: [], target: 'mock', synthetic: true, created_at: new Date(Date.UTC(2026, 9, 20 + i, 9)).toISOString() }));

/* ------------------------------------------------------------------- runs */

type RunStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled';

interface Run {
  id: string;
  input: RunInput;
  scenarioName: string;
  status: RunStatus;
  progress: number;
  phase_text: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  durationMs: number;
  startReal: number;
  results: ReturnType<typeof computeResults> | null;
  log: { id: number; event: string; data: string }[];
  clients: Set<ServerResponse>;
}

const PHASES = ['Registering users', 'Opening the window', 'Entries arriving', 'Drawing', 'Claiming seats', 'Computing metrics'];

function validateParams(schema: { properties: Record<string, Record<string, unknown>>; required?: readonly string[] }, values: Record<string, unknown>): string | null {
  for (const [k, v] of Object.entries(values)) {
    const s = schema.properties[k];
    if (!s) return `unknown parameter: ${k}`;
    if (s.type === 'integer' && !(typeof v === 'number' && Number.isInteger(v))) return `${k} must be a whole number`;
    if (s.type === 'number' && typeof v !== 'number') return `${k} must be a number`;
    if (s.type === 'boolean' && typeof v !== 'boolean') return `${k} must be true or false`;
    if (s.type === 'string' && typeof v !== 'string') return `${k} must be text`;
    if (typeof v === 'number' && typeof s.minimum === 'number' && v < s.minimum) return `${k} must be at least ${s.minimum}`;
    if (typeof v === 'number' && typeof s.maximum === 'number' && v > s.maximum) return `${k} must be at most ${s.maximum}`;
    if (Array.isArray(s.enum) && !s.enum.includes(v)) return `${k} must be one of ${s.enum.join(', ')}`;
  }
  return null;
}

export function registerSim(app: FastifyInstance, opts: { speed?: number } = {}): void {
  const runs = new Map<string, Run>();
  let counter = 0;
  const speed = opts.speed ?? Number(process.env.MOCK_SIM_SPEED ?? 1);

  const push = (run: Run, event: string, payload: unknown) => {
    const entry = { id: run.log.length + 1, event, data: JSON.stringify(payload) };
    run.log.push(entry);
    for (const res of run.clients) res.write(`id: ${entry.id}\nevent: ${entry.event}\ndata: ${entry.data}\n\n`);
  };

  const view = (run: Run) => ({
    run_id: run.id,
    scenario_id: run.input.scenario_id,
    status: run.status,
    progress: Math.round(run.progress * 1000) / 1000,
    phase_text: run.phase_text,
    created_at: run.created_at,
    started_at: run.started_at,
    finished_at: run.finished_at,
    target: 'mock',
  });

  const finish = (run: Run, status: RunStatus) => {
    run.status = status;
    run.finished_at = new Date().toISOString();
    if (status === 'done') {
      run.progress = 1;
      run.phase_text = 'Done';
      run.results = computeResults(run.id, run.input);
    }
    push(run, 'status', view(run));
    for (const res of run.clients) res.end();
    run.clients.clear();
  };

  const ticker = setInterval(() => {
    for (const run of runs.values()) {
      if (run.status === 'queued' && Date.now() - run.startReal > 600 / speed) {
        run.status = 'running';
        run.started_at = new Date().toISOString();
        push(run, 'status', view(run));
      }
      if (run.status !== 'running') continue;
      const elapsed = Date.now() - run.startReal;
      run.progress = Math.min(0.999, elapsed / run.durationMs);
      const rep = Math.min(run.input.repeats, Math.floor(run.progress * run.input.repeats) + 1);
      run.phase_text = `Repeat ${rep} of ${run.input.repeats}: ${PHASES[Math.floor(((run.progress * run.input.repeats) % 1) * PHASES.length)]}`;
      const final = computeResults(run.id, run.input);
      const share = final.metrics.fairness.bot_seat_share.mean;
      push(run, 'progress', { status: run.status, progress: Math.round(run.progress * 1000) / 1000, phase_text: run.phase_text });
      push(run, 'snapshot', {
        t_s: Math.round(elapsed / 100) / 10,
        requests: Math.round(run.progress * final.population.legit * 6 * run.input.repeats + run.progress * final.population.bots * 4000),
        throughput_rps: Math.round(final.metrics.system.throughput_rps.mean * (0.9 + 0.2 * Math.sin(elapsed / 700))),
        p95_ms: final.metrics.system.latency_ms.enter.p95,
        bot_seat_share: Math.round(share * (0.85 + 0.3 * Math.sin(elapsed / 900) ** 2) * 1e4) / 1e4,
      });
      if (elapsed >= run.durationMs) finish(run, 'done');
    }
  }, 500);
  ticker.unref();
  app.addHook('onClose', async () => clearInterval(ticker));

  const runOr404 = (req: FastifyRequest, reply: FastifyReply): Run | null => {
    const run = runs.get((req.params as { id: string }).id);
    if (!run) {
      sendError(reply, 404, 'NOT_FOUND', 'No such run');
      return null;
    }
    return run;
  };

  app.get('/sim/scenarios', async () => SIM_SCENARIOS);

  app.post('/sim/runs', async (req, reply) => {
    const b = (req.body ?? {}) as { scenario_id?: unknown; overrides?: unknown; repeats?: unknown; seed?: unknown; target?: unknown };
    const scenario = SIM_SCENARIOS.find((s) => s.id === b.scenario_id);
    if (!scenario) return sendError(reply, 400, 'VALIDATION_ERROR', `Unknown scenario: ${String(b.scenario_id)}`, { field: 'scenario_id' });
    if (b.target !== 'mock' && b.target !== 'real') return sendError(reply, 400, 'VALIDATION_ERROR', 'target must be "mock" or "real"', { field: 'target' });
    if (b.target === 'real') {
      return sendError(reply, 409, 'VALIDATION_ERROR', 'This is the mock simulator: it can only run target "mock". Point SIM_URL at the real simulator to run against the real stack.', { field: 'target' });
    }
    const repeats = b.repeats ?? 5;
    if (typeof repeats !== 'number' || !Number.isInteger(repeats) || repeats < 1 || repeats > 50) {
      return sendError(reply, 400, 'VALIDATION_ERROR', 'repeats must be 1 to 50', { field: 'repeats' });
    }
    const seed = b.seed ?? 42;
    if (typeof seed !== 'number' || !Number.isInteger(seed)) return sendError(reply, 400, 'VALIDATION_ERROR', 'seed must be an integer', { field: 'seed' });
    const overrides = (b.overrides ?? {}) as Record<string, unknown>;
    if (typeof overrides !== 'object' || Array.isArray(overrides)) return sendError(reply, 400, 'VALIDATION_ERROR', 'overrides must be an object');
    const problem = validateParams(scenario.params_schema as never, overrides);
    if (problem) return sendError(reply, 400, 'VALIDATION_ERROR', problem, { field: 'overrides' });

    const defaults = Object.fromEntries(Object.entries(scenario.params_schema.properties).map(([k, s]) => [k, (s as { default?: unknown }).default]));
    counter += 1;
    const id = `run_${String(counter).padStart(4, '0')}`;
    const run: Run = {
      id,
      input: { scenario_id: scenario.id, params: { ...defaults, ...overrides }, repeats, seed },
      scenarioName: scenario.name,
      status: 'queued',
      progress: 0,
      phase_text: 'Queued',
      created_at: new Date().toISOString(),
      started_at: null,
      finished_at: null,
      durationMs: (scenario.estimated_duration_s * 1000) / speed,
      startReal: Date.now(),
      results: null,
      log: [],
      clients: new Set(),
    };
    runs.set(id, run);
    return reply.code(201).send({ run_id: id });
  });

  /** Not in the contract yet (requested as C1): recent runs, newest first. */
  app.get('/sim/runs', async () =>
    [...runs.values()].reverse().map((r) => ({
      ...view(r),
      scenario_name: r.scenarioName,
      params: r.input.params,
      repeats: r.input.repeats,
      seed: r.input.seed,
    })),
  );

  app.get('/sim/runs/:id', async (req, reply) => {
    const run = runOr404(req, reply);
    if (!run) return;
    return view(run);
  });

  app.get('/sim/runs/:id/results', async (req, reply) => {
    const run = runOr404(req, reply);
    if (!run) return;
    if (!run.results) return sendError(reply, 409, 'VALIDATION_ERROR', `Results are available when the run is done (it is ${run.status})`);
    return run.results;
  });

  app.post('/sim/runs/:id/cancel', async (req, reply) => {
    const run = runOr404(req, reply);
    if (!run) return;
    if (run.status === 'queued' || run.status === 'running') finish(run, 'cancelled');
    return view(run);
  });

  app.get('/sim/runs/:id/stream', async (req, reply) => {
    const run = runOr404(req, reply);
    if (!run) return;
    reply.hijack();
    const res = reply.raw;
    res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8', 'Cache-Control': 'no-cache, no-transform', Connection: 'keep-alive', 'X-Accel-Buffering': 'no' });
    res.write('retry: 2000\n\n');
    const lastId = Number.parseInt(String(req.headers['last-event-id'] ?? ''), 10);
    const replay = Number.isFinite(lastId) ? run.log.filter((e) => e.id > lastId) : [];
    for (const e of replay) res.write(`id: ${e.id}\nevent: ${e.event}\ndata: ${e.data}\n\n`);
    if (!Number.isFinite(lastId)) {
      // Fresh connection: current status first, so the page never starts blank.
      const id = run.log.length;
      res.write(`id: ${id}\nevent: status\ndata: ${JSON.stringify(view(run))}\n\n`);
    }
    if (run.status !== 'queued' && run.status !== 'running') {
      res.end();
      return;
    }
    run.clients.add(res);
    res.on('close', () => run.clients.delete(res));
  });

  app.get('/sim/experiments', async () => EXPERIMENTS);

  app.get('/sim/experiments/:id', async (req, reply) => {
    const e = EXPERIMENTS.find((x) => x.id === (req.params as { id: string }).id);
    if (!e) return sendError(reply, 404, 'NOT_FOUND', 'No such experiment');
    return e;
  });

  app.get('/sim/charts/:file', async (req, reply) => {
    const file = (req.params as { file: string }).file;
    const png = file.endsWith('.png');
    const chartId = png ? file.slice(0, -4) : file;
    const chart = CHARTS[chartId];
    if (!chart) return sendError(reply, 404, 'NOT_FOUND', `No such chart: ${chartId}`);
    if (png) return reply.type('image/png').send(renderChartPng(chart.series));
    return { chart_id: chartId, ...chart, target: 'mock', synthetic: true };
  });
}
