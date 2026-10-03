/**
 * Public fairness and audit responses (A). Shapes beyond the shared conventions
 * are provisional and filed as A15/A16 in docs/INTERFACE_REQUESTS_D.md.
 */
import { z } from 'zod';
import { isoUtc, phaseSchema } from '@/api/schemas';

export const hex64 = z.string().regex(/^[0-9a-f]{64}$/, 'expected 64 lowercase hex characters');

export const fairnessSchema = z.object({
  event_id: z.string(),
  phase: phaseSchema,
  algorithm_version: z.string(),
  seed_commitment: hex64,
  /** Revealed only after the draw. */
  server_seed: hex64.nullable(),
  beacon: z
    .object({
      source: z.string(),
      round: z.number().int().nonnegative(),
      /** Known only once the round has been published. */
      randomness: hex64.nullable(),
    })
    .nullable(),
  /** Published when the window closes. */
  entrants_hash: hex64.nullable(),
  entrants_count: z.number().int().nonnegative().nullable(),
  final_seed: hex64.nullable(),
  inventory: z.number().int().nonnegative(),
  result: z
    .object({
      winners_count: z.number().int().nonnegative(),
      waitlist_count: z.number().int().nonnegative(),
      winners_hash: hex64,
      waitlist_hash: hex64,
    })
    .nullable(),
  audit_head_hash: hex64.nullable(),
  synthetic: z.boolean().optional(),
  server_now: isoUtc,
});
export type Fairness = z.infer<typeof fairnessSchema>;

export const entrantsSchema = z.object({
  event_id: z.string(),
  count: z.number().int().nonnegative(),
  entrants: z.array(z.object({ public_id: z.string(), weight: z.number() })),
});
export type EntrantsResponse = z.infer<typeof entrantsSchema>;

export const resultsSchema = z.object({
  event_id: z.string(),
  winners: z.array(z.string()),
  waitlist: z.array(z.string()),
});
export type ResultsResponse = z.infer<typeof resultsSchema>;

export const auditRecordSchema = z.object({
  seq: z.number().int().positive(),
  type: z.string(),
  ts: isoUtc,
  // Hashed exactly as received, so it must not be reshaped by validation.
  payload: z.unknown(),
  prev_hash: hex64,
  hash: hex64,
});
export type AuditRecord = z.infer<typeof auditRecordSchema>;

export const auditPageSchema = z.object({
  event_id: z.string(),
  records: z.array(auditRecordSchema),
  next_from_seq: z.number().int().positive().nullable(),
  head: z.object({ seq: z.number().int().nonnegative(), hash: hex64 }).nullable(),
});
export type AuditPage = z.infer<typeof auditPageSchema>;

export const auditVerifySchema = z.object({
  ok: z.boolean(),
  head_seq: z.number().int().nonnegative(),
  head_hash: hex64,
  checked: z.number().int().nonnegative(),
  first_bad_seq: z.number().int().positive().nullable().optional(),
});
export type AuditVerify = z.infer<typeof auditVerifySchema>;
