import { ApiClient } from '@/api/client';
import { serverClock } from '@/lib/serverClock';
import { getDeviceId } from '@/lib/storage';
import { z } from 'zod';
import { chartSchema, experimentSchema, resultsSchema, runListSchema, runSchema, scenarioSchema } from './schemas';

/** Where the simulator lives: /sim on the same origin (nginx and Vite both route it to C's service). */
export const SIM_BASE = '/sim';

const client = new ApiClient({ baseUrl: SIM_BASE, getToken: () => null, getDeviceId, clock: serverClock, defaultTimeoutMs: 20_000 });
const enc = encodeURIComponent;

export interface StartRun {
  scenario_id: string;
  overrides: Record<string, unknown>;
  repeats: number;
  seed: number;
  target: 'real' | 'mock';
}

export const simApi = {
  scenarios: (signal?: AbortSignal) => client.request('/scenarios', { schema: z.array(scenarioSchema), auth: false, signal }),
  start: (body: StartRun) => client.request('/runs', { method: 'POST', body, schema: z.object({ run_id: z.string() }), auth: false }),
  runs: (signal?: AbortSignal) => client.request('/runs', { schema: runListSchema, auth: false, signal }),
  run: (id: string, signal?: AbortSignal) => client.request(`/runs/${enc(id)}`, { schema: runSchema, auth: false, signal }),
  results: (id: string, signal?: AbortSignal) => client.request(`/runs/${enc(id)}/results`, { schema: resultsSchema, auth: false, signal }),
  cancel: (id: string) => client.request(`/runs/${enc(id)}/cancel`, { method: 'POST', schema: runSchema, auth: false }),
  experiments: (signal?: AbortSignal) => client.request('/experiments', { schema: z.array(experimentSchema), auth: false, signal }),
  chart: (chartId: string, experimentId: string, signal?: AbortSignal) =>
    client.request(`/charts/${enc(chartId)}?experiment=${enc(experimentId)}`, { schema: chartSchema, auth: false, signal }),
  chartPngUrl: (chartId: string, experimentId: string) => `${SIM_BASE}/charts/${enc(chartId)}.png?experiment=${enc(experimentId)}`,
  streamUrl: (id: string) => `${SIM_BASE}/runs/${enc(id)}/stream`,
};

export const simKeys = {
  scenarios: ['sim', 'scenarios'] as const,
  runs: ['sim', 'runs'] as const,
  run: (id: string) => ['sim', 'runs', id] as const,
  results: (id: string) => ['sim', 'runs', id, 'results'] as const,
  experiments: ['sim', 'experiments'] as const,
  chart: (chartId: string, exp: string) => ['sim', 'charts', chartId, exp] as const,
};
