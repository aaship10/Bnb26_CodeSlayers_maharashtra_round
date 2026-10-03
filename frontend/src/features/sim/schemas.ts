/**
 * Simulator service (/sim, Member C). Shapes follow the shared contract
 * (section 5); fields the contract leaves open are typed loosely and noted.
 */
import { z } from 'zod';

/** A measured value: always a mean WITH its 95% CI and sample size. */
export const statSchema = z.object({
  mean: z.number(),
  ci_low: z.number(),
  ci_high: z.number(),
  n: z.number().int().positive(),
});
export type Stat = z.infer<typeof statSchema>;

/** Some fields may come as a Stat or as a single value; the UI labels the latter as having no CI. */
export const statOrNumber = z.union([statSchema, z.number()]);
export type StatOrNumber = z.infer<typeof statOrNumber>;

const jsonSchemaNode: z.ZodType<JsonSchema> = z.lazy(() =>
  z
    .object({
      type: z.string().optional(),
      title: z.string().optional(),
      description: z.string().optional(),
      default: z.unknown().optional(),
      enum: z.array(z.unknown()).optional(),
      minimum: z.number().optional(),
      maximum: z.number().optional(),
      multipleOf: z.number().optional(),
      properties: z.record(jsonSchemaNode).optional(),
      required: z.array(z.string()).optional(),
      items: jsonSchemaNode.optional(),
    })
    .passthrough(),
);

export interface JsonSchema {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  minimum?: number;
  maximum?: number;
  multipleOf?: number;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
}

export const scenarioSchema = z.object({
  id: z.string(),
  name: z.string(),
  description: z.string(),
  profile: z.string(),
  params_schema: jsonSchemaNode,
  estimated_duration_s: z.number().nonnegative(),
  /** Free-form in the contract ("50,000 logical users", or a number). */
  scale: z.union([z.string(), z.number(), z.record(z.unknown())]).optional(),
});
export type Scenario = z.infer<typeof scenarioSchema>;

export const RUN_STATUSES = ['queued', 'running', 'done', 'failed', 'cancelled'] as const;
export const runStatusSchema = z.enum(RUN_STATUSES);
export type RunStatus = z.infer<typeof runStatusSchema>;

export const runSchema = z.object({
  run_id: z.string().optional(),
  scenario_id: z.string().optional(),
  status: runStatusSchema,
  progress: z.number().min(0).max(1),
  phase_text: z.string().nullable().optional(),
  started_at: z.string().nullable().optional(),
  finished_at: z.string().nullable().optional(),
  target: z.enum(['real', 'mock']).optional(),
});
export type Run = z.infer<typeof runSchema>;

/** Requested from C as C1 (GET /sim/runs). */
export const runListSchema = z.array(
  runSchema.extend({
    run_id: z.string(),
    scenario_id: z.string(),
    scenario_name: z.string().optional(),
    params: z.record(z.unknown()).optional(),
    repeats: z.number().int().optional(),
    seed: z.number().int().optional(),
    created_at: z.string().optional(),
  }),
);
export type RunSummary = z.infer<typeof runListSchema>[number];

const pct = z.object({ p50: statOrNumber, p95: statOrNumber, p99: statOrNumber });

export const resultsSchema = z.object({
  schema_version: z.literal(1),
  run_id: z.string(),
  scenario_id: z.string(),
  target: z.enum(['real', 'mock']),
  synthetic: z.boolean(),
  seed: z.number(),
  repeats: z.number().int().positive(),
  population: z.object({ legit: z.number().int().nonnegative(), bots: z.number().int().nonnegative(), bot_identities: z.number().int().nonnegative() }),
  event: z.object({
    inventory: z.number().int().nonnegative(),
    mode: z.enum(['LOTTERY', 'FCFS']),
    defences: z.union([z.string(), z.object({ preset: z.string() }).passthrough()]),
  }),
  metrics: z.object({
    fairness: z.object({
      bot_seat_share: statSchema,
      bot_entrant_share: statSchema,
      human_win_prob: statSchema,
      human_entry_success_rate: statSchema,
      arrival_time_correlation: statSchema,
      jain_index: statSchema,
      gini: statSchema,
      attacker_cost_per_seat: z.object({ requests: statOrNumber.nullable(), accounts: statOrNumber.nullable(), pow_hashes: statOrNumber.nullable() }),
    }),
    system: z.object({
      latency_ms: z.object({ enter: pct, status: pct, claim: pct }),
      throughput_rps: statOrNumber,
      error_rates: z.object({ http_429_legit: statOrNumber, http_429_bot: statOrNumber, http_5xx: statOrNumber, timeout: statOrNumber }),
    }),
    detection: z.object({ precision: statOrNumber.nullable(), recall: statOrNumber.nullable(), false_positive_rate: statOrNumber.nullable() }),
    integrity: z.object({
      oversold: z.number().int().nonnegative(),
      duplicate_users: z.number().int().nonnegative(),
      duplicate_seats: z.number().int().nonnegative(),
      orphaned_holds: z.number().int().nonnegative(),
      draw_verified: z.boolean().nullable(),
      passed: z.boolean(),
    }),
  }),
});
export type SimResults = z.infer<typeof resultsSchema>;

export const experimentSchema = z.object({
  id: z.string(),
  title: z.string(),
  description: z.string(),
  charts: z.array(z.string()),
  run_ids: z.array(z.string()),
  target: z.enum(['real', 'mock']),
  synthetic: z.boolean(),
  created_at: z.string(),
});
export type Experiment = z.infer<typeof experimentSchema>;

export const chartSchema = z.object({
  chart_id: z.string(),
  title: z.string(),
  x_label: z.string(),
  y_label: z.string(),
  series: z.array(
    z.object({
      name: z.string(),
      points: z.array(z.object({ x: z.union([z.number(), z.string()]), y: z.number(), ci_low: z.number().optional(), ci_high: z.number().optional() })),
    }),
  ),
  notes: z.string().optional(),
  target: z.enum(['real', 'mock']),
  synthetic: z.boolean(),
});
export type ChartData = z.infer<typeof chartSchema>;

/** Live snapshot pushed on the run stream (shape not fixed by the contract; filed as C2). */
export const snapshotSchema = z
  .object({
    t_s: z.number().optional(),
    requests: z.number().optional(),
    throughput_rps: z.number().optional(),
    p95_ms: z.number().optional(),
    bot_seat_share: z.number().optional(),
  })
  .passthrough();
export type Snapshot = z.infer<typeof snapshotSchema>;
