/**
 * Runtime validation for every API response (shared conventions, sections 3 and 4).
 *
 * Field names follow the agreed contract. Where the contract is silent the
 * assumption is listed in docs/INTERFACE_REQUESTS_D.md; regenerate/adjust once
 * docs/openapi.json lands.
 *
 * zod strips unknown keys, so a stray `sim_label` from a careless backend can
 * never reach the UI: ground truth is simulator-only and we never model it.
 */
import { z } from 'zod';

/** ISO-8601 UTC with a trailing Z (the agreed wire format). */
export const isoUtc = z.string().datetime();

export const PHASES = ['DRAFT', 'SCHEDULED', 'OPEN', 'DRAWING', 'CLAIMING', 'CLOSED'] as const;
export const phaseSchema = z.enum(PHASES);
export type Phase = z.infer<typeof phaseSchema>;

export const USER_STATES = ['REGISTERED', 'ENTERED', 'WON', 'WAITLISTED', 'LOST', 'CLAIMED', 'EXPIRED'] as const;
export const userStateSchema = z.enum(USER_STATES);
export type UserState = z.infer<typeof userStateSchema>;

export const modeSchema = z.enum(['LOTTERY', 'FCFS']);
export type EventMode = z.infer<typeof modeSchema>;

/* ---------- errors ---------- */

export const errorBodySchema = z.object({
  code: z.string(),
  message: z.string(),
  details: z.record(z.unknown()).optional(),
});
export type ErrorBody = z.infer<typeof errorBodySchema>;

/* ---------- challenges (B) ---------- */

export const powParamsSchema = z.object({
  algo: z.literal('sha256-lzb'),
  prefix: z.string().min(1),
  difficulty_bits: z.number().int().min(0).max(64),
});

export const captchaParamsSchema = z.object({
  provider: z.string(),
  site_key: z.string(),
});

export const challengeSchema = z
  .object({
    id: z.string().min(1),
    type: z.enum(['pow', 'captcha']),
    expires_at: isoUtc,
    pow: powParamsSchema.optional(),
    captcha: captchaParamsSchema.optional(),
  })
  .superRefine((c, ctx) => {
    if (c.type === 'pow' && !c.pow) ctx.addIssue({ code: 'custom', message: 'pow challenge without pow params' });
    if (c.type === 'captcha' && !c.captcha) {
      ctx.addIssue({ code: 'custom', message: 'captcha challenge without captcha params' });
    }
  });
export type Challenge = z.infer<typeof challengeSchema>;

/* ---------- auth (B) ---------- */

export const sessionResponseSchema = z.object({
  token: z.string().min(1),
  expires_at: isoUtc,
  user_id: z.string().min(1),
});
export type SessionResponse = z.infer<typeof sessionResponseSchema>;

export const meSchema = z.object({
  user_id: z.string().min(1),
  email: z.string(),
  display_name: z.string(),
});
export type Me = z.infer<typeof meSchema>;

/* ---------- events (A) ---------- */

export const eventSchema = z.object({
  id: z.string().min(1),
  name: z.string(),
  description: z.string().optional(),
  phase: phaseSchema,
  mode: modeSchema,
  /** Seats on offer (N). */
  inventory: z.number().int().nonnegative(),
  window_opens_at: isoUtc,
  window_closes_at: isoUtc,
  claim_ttl_s: z.number().int().positive(),
  seed_commitment: z.string().nullable().optional(),
  server_now: isoUtc,
});
export type EventInfo = z.infer<typeof eventSchema>;

export const eventListSchema = z.array(eventSchema);

export const enterResponseSchema = z.object({
  state: userStateSchema,
  entered_at: isoUtc,
  already_entered: z.boolean(),
});
export type EnterResponse = z.infer<typeof enterResponseSchema>;

/**
 * GET /events/{id}/status. Deliberately no rank/draw fields before the draw:
 * waitlist_position only exists after it, seat_no only when claimed.
 * public_id and ticket_code are requested additions (see INTERFACE_REQUESTS_D.md).
 */
export const statusSchema = z.object({
  state: userStateSchema,
  phase: phaseSchema,
  hold_expires_at: isoUtc.optional(),
  seat_no: z.number().int().positive().optional(),
  ticket_code: z.string().optional(),
  waitlist_position: z.number().int().positive().optional(),
  public_id: z.string().optional(),
  server_now: isoUtc,
});
export type StatusResponse = z.infer<typeof statusSchema>;

export const claimResponseSchema = z.object({
  state: z.literal('CLAIMED'),
  seat_no: z.number().int().positive(),
  ticket_code: z.string().min(1),
});
export type ClaimResponse = z.infer<typeof claimResponseSchema>;
