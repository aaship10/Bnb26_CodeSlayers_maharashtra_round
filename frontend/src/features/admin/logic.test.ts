import { PHASES } from '@/api/schemas';
import { ApiError } from '@/api/errors';
import { actionEnabled, fromLocalInput, matchPreset, nextAction, paramsSummary, pollInterval, toLocalInput, toggleLayer } from './logic';
import { LIFECYCLE, type DefenceConfig, type Preset } from './schemas';
import { validateCreate } from './CreateEventDialog';

const layer = (enabled: boolean, extra: Record<string, unknown> = {}) => ({ enabled, ...extra });
const cfg = (on: string[], preset: DefenceConfig['preset'] = 'custom'): DefenceConfig => ({
  preset,
  layers: {
    rate_limit: layer(on.includes('rate_limit'), { per_ip_rps: 5 }),
    pow: layer(on.includes('pow'), { difficulty_bits: 18 }),
    captcha: layer(on.includes('captcha')),
    signals: layer(on.includes('signals')),
    risk: layer(on.includes('risk')),
  },
});
const PRESETS: Preset[] = [
  { id: 'none', name: 'None', description: '', defences: cfg([], 'none') },
  { id: 'rate_limit', name: 'RL', description: '', defences: cfg(['rate_limit'], 'rate_limit') },
  { id: 'rate_limit+pow', name: 'RL+PoW', description: '', defences: cfg(['rate_limit', 'pow'], 'rate_limit+pow') },
];

describe('lifecycle rules', () => {
  it('each action is enabled in exactly one phase', () => {
    for (const action of LIFECYCLE) expect(PHASES.filter((p) => actionEnabled(action, p))).toHaveLength(1);
  });

  it('follows DRAFT -> SCHEDULED -> OPEN -> DRAWING -> CLAIMING', () => {
    expect(actionEnabled('schedule', 'DRAFT')).toBe(true);
    expect(actionEnabled('open', 'SCHEDULED')).toBe(true);
    expect(actionEnabled('close', 'OPEN')).toBe(true);
    expect(actionEnabled('draw', 'DRAWING')).toBe(true);
    expect(actionEnabled('draw', 'OPEN')).toBe(false); // can't draw while entries are open
    expect(actionEnabled('open', 'CLOSED')).toBe(false);
  });

  it('nothing to do once claiming or closed', () => {
    expect(nextAction('CLAIMING')).toBeNull();
    expect(nextAction('CLOSED')).toBeNull();
    expect(nextAction('OPEN')).toBe('close');
  });
});

describe('defence presets', () => {
  it('recognises a config that equals a preset, including parameters', () => {
    expect(matchPreset(cfg(['rate_limit', 'pow']).layers, PRESETS)).toBe('rate_limit+pow');
    expect(matchPreset(cfg([]).layers, PRESETS)).toBe('none');
  });

  it('a changed parameter makes it custom even if the switches match', () => {
    const c = cfg(['rate_limit']);
    c.layers.rate_limit = { enabled: true, per_ip_rps: 50 };
    expect(matchPreset(c.layers, PRESETS)).toBe('custom');
  });

  it('flipping a layer moves to the matching preset or to custom, keeping parameters', () => {
    const start = cfg(['rate_limit'], 'rate_limit');
    const withPow = toggleLayer(start, 'pow', true, PRESETS);
    expect(withPow.preset).toBe('rate_limit+pow');
    expect(withPow.layers.pow).toEqual({ enabled: true, difficulty_bits: 18 });
    const odd = toggleLayer(start, 'captcha', true, PRESETS);
    expect(odd.preset).toBe('custom');
    expect(start.layers.captcha.enabled).toBe(false); // input not mutated
  });

  it('summarises parameters without "enabled"', () => {
    expect(paramsSummary({ enabled: true, per_ip_rps: 5, burst: 10 })).toBe('per_ip_rps 5 · burst 10');
    expect(paramsSummary({ enabled: false })).toBe('');
  });
});

describe('organizer polling interval', () => {
  const healthy = { state: { fetchFailureCount: 0, error: null } } as never;
  it('every base..base+spread while healthy (never per-second)', () => {
    expect(pollInterval(3000, 2000, { rng: () => 0 })(healthy)).toBe(3000);
    expect(pollInterval(3000, 2000, { rng: () => 0.999 })(healthy)).toBeLessThanOrEqual(5000);
  });

  it('backs off while failing, up to a minute, and honours Retry-After', () => {
    const failing = (n: number, error: unknown = new ApiError('INTERNAL', 'x', 500)) => ({ state: { fetchFailureCount: n, error } }) as never;
    const f = pollInterval(3000, 2000, { rng: () => 0 });
    expect(f(failing(1))).toBeGreaterThanOrEqual(3000);
    expect(f(failing(4))).toBeGreaterThan(f(failing(1)));
    expect(f(failing(20))).toBeLessThanOrEqual(60_000);
    expect(f(failing(1, new ApiError('RATE_LIMITED', 'x', 429, undefined, 30_000)))).toBeGreaterThanOrEqual(30_000);
  });
});

describe('poll interval stability (regression: timers that never fired)', () => {
  const q = (dataUpdatedAt: number, fetchFailureCount = 0) => ({ state: { dataUpdatedAt, errorUpdatedAt: 0, fetchFailureCount, error: null } }) as never;

  it('returns the SAME value for the same query state, however often it is evaluated', () => {
    const f = pollInterval(5000, 3000);
    const first = f(q(1_700_000_123_456));
    for (let i = 0; i < 50; i++) expect(f(q(1_700_000_123_456))).toBe(first);
  });

  it('still spreads clients: different update times give different delays within the window', () => {
    const f = pollInterval(5000, 3000);
    const values = new Set(Array.from({ length: 200 }, (_, i) => f(q(1_700_000_000_000 + i * 37))));
    expect(values.size).toBeGreaterThan(100);
    for (const v of values) {
      expect(v).toBeGreaterThanOrEqual(5000);
      expect(v).toBeLessThanOrEqual(8000);
    }
  });
});

describe('create form', () => {
  it('local datetime input round-trips through UTC', () => {
    const ms = Date.parse('2026-11-01T10:05:00.000Z');
    expect(fromLocalInput(toLocalInput(ms))).toBe('2026-11-01T10:05:00.000Z');
    expect(fromLocalInput('')).toBeNull();
  });

  const good = { name: 'Night', description: '', inventory: '500', opens: '2026-11-01T10:00', closes: '2026-11-01T10:30', ttlMin: '10', mode: 'LOTTERY' as const, preset: 'rate_limit' as const };

  it('accepts a sensible event', () => {
    expect(validateCreate(good)).toEqual({});
  });

  it('explains each problem next to its field', () => {
    const e = validateCreate({ ...good, name: ' ', inventory: '0', closes: '2026-11-01T09:00', ttlMin: '0' });
    expect(Object.keys(e).sort()).toEqual(['closes', 'inventory', 'name', 'ttlMin']);
    expect(e.closes).toMatch(/after it opens/);
    expect(validateCreate({ ...good, inventory: '2.5' }).inventory).toBeDefined();
  });
});
