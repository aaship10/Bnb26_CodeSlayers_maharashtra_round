import { z } from 'zod';
import { buildApp } from './src/app';
import { World } from './src/world';
import { botSeatShare } from './src/sim';
import { chartSchema, experimentSchema, resultsSchema, runListSchema, runSchema, scenarioSchema } from '../src/features/sim/schemas';
import { parseParamsSchema } from '../src/features/sim/jsonSchemaForm';
import { errorBodySchema } from '../src/api/schemas';

// Runs finish ~100x faster than in the dev server.
const make = () => buildApp(new World(), { simSpeed: 100 });
type App = ReturnType<typeof make>['app'];

const json = async (app: App, url: string) => (await app.inject({ method: 'GET', url })).json();

async function runToEnd(app: App, body: Record<string, unknown>) {
  const res = await app.inject({ method: 'POST', url: '/sim/runs', payload: { repeats: 5, seed: 42, target: 'mock', overrides: {}, ...body } });
  expect(res.statusCode).toBe(201);
  const { run_id } = z.object({ run_id: z.string() }).parse(res.json());
  for (let i = 0; i < 60; i++) {
    const run = runSchema.parse(await json(app, `/sim/runs/${run_id}`));
    if (run.status === 'done') return resultsSchema.parse(await json(app, `/sim/runs/${run_id}/results`));
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error('run did not finish');
}

describe('mock simulator contract', () => {
  it('scenarios are schema-valid and every parameter is editable by the generated form', async () => {
    const { app } = make();
    const scenarios = z.array(scenarioSchema).parse(await json(app, '/sim/scenarios'));
    expect(scenarios.length).toBeGreaterThanOrEqual(4);
    for (const s of scenarios) expect(parseParamsSchema(s.params_schema).unsupported, s.id).toEqual([]);
    await app.close();
  });

  it('a run goes queued -> running -> done and its results validate, labelled mock + synthetic', async () => {
    const { app } = make();
    const r = await runToEnd(app, { scenario_id: 'bot_swarm' });
    expect(r).toMatchObject({ target: 'mock', synthetic: true, repeats: 5, schema_version: 1 });
    expect(r.metrics.fairness.bot_seat_share.n).toBe(5);
    expect(r.metrics.fairness.bot_seat_share.ci_low).toBeLessThanOrEqual(r.metrics.fairness.bot_seat_share.mean);
    expect(runListSchema.parse(await json(app, '/sim/runs'))[0]!.status).toBe('done');
    await app.close();
  }, 20_000);

  it('rejects bad input and refuses target "real" (it is a mock)', async () => {
    const { app } = make();
    const bad = await app.inject({ method: 'POST', url: '/sim/runs', payload: { scenario_id: 'bot_swarm', target: 'mock', overrides: { bots: 99_999 } } });
    expect(bad.statusCode).toBe(400);
    expect(errorBodySchema.parse(bad.json()).message).toMatch(/at most/);
    const real = await app.inject({ method: 'POST', url: '/sim/runs', payload: { scenario_id: 'bot_swarm', target: 'real', overrides: {} } });
    expect(real.statusCode).toBe(409);
    expect(errorBodySchema.parse(real.json()).message).toMatch(/mock/);
    await app.close();
  });

  it('cancel stops a run', async () => {
    const { app } = buildApp(new World(), { simSpeed: 0.01 });
    const { run_id } = (await app.inject({ method: 'POST', url: '/sim/runs', payload: { scenario_id: 'flash_crowd', target: 'mock' } })).json();
    const res = runSchema.parse((await app.inject({ method: 'POST', url: `/sim/runs/${run_id}/cancel` })).json());
    expect(res.status).toBe('cancelled');
    expect((await app.inject({ method: 'GET', url: `/sim/runs/${run_id}/results` })).statusCode).toBe(409);
    await app.close();
  });

  it('experiments and chart datasets validate; PNG fallback is a real PNG', async () => {
    const { app } = make();
    const exps = z.array(experimentSchema).parse(await json(app, '/sim/experiments'));
    const ids = new Set(exps.flatMap((e) => e.charts));
    for (const id of [
      'bot_share_vs_request_multiplier',
      'bot_share_vs_identities',
      'latency_percentiles_normal_vs_attack',
      'detection_precision_recall_by_layer',
      'ablation_bot_share',
      'human_win_prob_under_attack',
    ]) {
      expect(ids.has(id), id).toBe(true);
      const c = chartSchema.parse(await json(app, `/sim/charts/${id}?experiment=${exps[0]!.id}`));
      expect(c.synthetic).toBe(true);
      for (const s of c.series) for (const p of s.points) expect(p.ci_low! <= p.y && p.y <= p.ci_high!, `${id} ${s.name}`).toBe(true);
    }
    const png = await app.inject({ method: 'GET', url: '/sim/charts/ablation_bot_share.png?experiment=exp_ablation' });
    expect(png.headers['content-type']).toBe('image/png');
    expect([...png.rawPayload.subarray(0, 8)]).toEqual([137, 80, 78, 71, 13, 10, 26, 10]);
    await app.close();
  });
});

describe('the story the synthetic model tells (what the demo relies on)', () => {
  it('FCFS: bots take almost every seat; Fair Drop under the same attack: they take about their share of identities', () => {
    const fcfs = botSeatShare('FCFS', 50_000, 200, 100, 'none', 500);
    const lottery = botSeatShare('LOTTERY', 50_000, 200, 100, 'none', 500);
    expect(fcfs).toBeGreaterThan(0.9);
    expect(lottery).toBeCloseTo(200 / 50_200, 6);
  });

  it('Fair Drop: 100x more requests does not move the allocation', () => {
    expect(botSeatShare('LOTTERY', 50_000, 200, 1000, 'none', 500)).toBe(botSeatShare('LOTTERY', 50_000, 200, 10, 'none', 500));
  });

  it('more sybil identities degrade it gradually, and defences bend the curve down', () => {
    const at = (ids: number, p: 'none' | 'all') => botSeatShare('LOTTERY', 50_000, ids, 1, p, 500);
    expect(at(10_000, 'none')).toBeGreaterThan(at(1_000, 'none'));
    expect(at(10_000, 'all')).toBeLessThan(at(10_000, 'none') / 2);
  });

  it('a replica kill keeps integrity unless the broken-build demo switch is on', async () => {
    const { app } = make();
    const ok = await runToEnd(app, { scenario_id: 'replica_kill' });
    expect(ok.metrics.integrity.passed).toBe(true);
    const broken = await runToEnd(app, { scenario_id: 'replica_kill', overrides: { simulate_broken_build: true } });
    expect(broken.metrics.integrity).toMatchObject({ passed: false, oversold: 3 });
    await app.close();
  }, 30_000);
});
