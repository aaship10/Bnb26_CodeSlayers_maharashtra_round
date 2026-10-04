/**
 * Organizer API shapes. Lives in the admin chunk so attendees never download it.
 * Fields beyond the shared conventions are assumptions filed in
 * docs/INTERFACE_REQUESTS_D.md (A10-A13, B4); adjust when openapi.json / CONFIG_SCHEMA.md land.
 */
import { z } from 'zod';
import { eventSchema, isoUtc, phaseSchema } from '@/api/schemas';

export const PRESET_IDS = ['none', 'rate_limit', 'rate_limit+pow', 'rate_limit+pow+captcha', 'all', 'custom'] as const;
export type PresetId = (typeof PRESET_IDS)[number];

export const LAYER_IDS = ['rate_limit', 'pow', 'captcha', 'signals', 'risk'] as const;
export type LayerId = (typeof LAYER_IDS)[number];

/** A layer is { enabled, ...params }; params are B's business, so they pass through untouched. */
export const layerSchema = z.object({ enabled: z.boolean() }).passthrough();
export type Layer = z.infer<typeof layerSchema>;

export const defenceConfigSchema = z.object({
  preset: z.enum(PRESET_IDS),
  layers: z.object({
    rate_limit: layerSchema,
    pow: layerSchema,
    captcha: layerSchema,
    signals: layerSchema,
    risk: layerSchema,
  }),
});
export type DefenceConfig = z.infer<typeof defenceConfigSchema>;

export const presetSchema = z.object({
  id: z.enum(PRESET_IDS),
  name: z.string(),
  description: z.string(),
  defences: defenceConfigSchema,
});
export const presetListSchema = z.array(presetSchema);
export type Preset = z.infer<typeof presetSchema>;

export const adminEventSchema = eventSchema.extend({
  config: z.object({ defences: defenceConfigSchema }),
});
export type AdminEvent = z.infer<typeof adminEventSchema>;
export const adminEventListSchema = z.array(adminEventSchema);

const count = z.number().int().nonnegative();

export const statsSchema = z.object({
  event_id: z.string(),
  phase: phaseSchema,
  by_state: z.object({
    REGISTERED: count,
    ENTERED: count,
    WON: count,
    WAITLISTED: count,
    CLAIMED: count,
    EXPIRED: count,
    LOST: count,
  }),
  entrants: count,
  allocations: z.object({ inventory: count, claimed: count, held: count, available: count }),
  holds: z.object({ active: count, expired: count }),
  /** Mock/simulated numbers. Anything true here wears the MOCK badge. */
  synthetic: z.boolean().optional(),
  server_now: isoUtc,
});
export type Stats = z.infer<typeof statsSchema>;

export const invariantsSchema = z.object({
  oversold: count,
  duplicate_users: count,
  duplicate_seats: count,
  orphaned_holds: count,
  passed: z.boolean(),
  checked_at: isoUtc,
  server_now: isoUtc.optional(),
});
export type Invariants = z.infer<typeof invariantsSchema>;

export const LIFECYCLE = ['schedule', 'open', 'close', 'draw'] as const;
export type LifecycleAction = (typeof LIFECYCLE)[number];

export interface CreateEventBody {
  name: string;
  description?: string;
  inventory: number;
  window_opens_at: string;
  window_closes_at: string;
  claim_ttl_s: number;
  mode: 'LOTTERY' | 'FCFS';
  config: { defences: DefenceConfig };
}
