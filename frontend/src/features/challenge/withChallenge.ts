import { ApiError, isApiError } from '@/api/errors';
import { challengeSchema, type Challenge } from '@/api/schemas';
import type { ChallengeSolution } from '@/api/client';

/** Something that can turn a Challenge into the value for X-Challenge-Solution. */
export interface ChallengeHandler {
  solve(challenge: Challenge, signal: AbortSignal): Promise<ChallengeSolution>;
}

export interface WithChallengeOptions {
  signal: AbortSignal;
  /** How many challenges we will solve for one request before giving up. */
  maxRounds?: number;
}

/**
 * Run `attempt`; if the server answers 403 CHALLENGE_REQUIRED, solve the
 * challenge it attached and run the SAME attempt again with the solution.
 *
 * "Same" matters: the caller's closure keeps its Idempotency-Key and body, so
 * the retry is the identical request plus two headers, never a new claim.
 * Bounded so a server that keeps issuing challenges can't trap the client in a loop.
 */
export async function withChallenges<T>(
  attempt: (solution?: ChallengeSolution) => Promise<T>,
  handler: ChallengeHandler,
  { signal, maxRounds = 3 }: WithChallengeOptions,
): Promise<T> {
  let solution: ChallengeSolution | undefined;
  for (let round = 0; ; round++) {
    try {
      return await attempt(solution);
    } catch (e) {
      if (!isApiError(e) || e.code !== 'CHALLENGE_REQUIRED') throw e;
      if (round >= maxRounds) throw e;
      const parsed = challengeSchema.safeParse(e.details?.challenge);
      if (!parsed.success) {
        console.error('[challenge] CHALLENGE_REQUIRED without a usable challenge', e.details, parsed.error.issues);
        throw new ApiError('SCHEMA_MISMATCH', 'CHALLENGE_REQUIRED carried no valid challenge', e.status, { issues: parsed.error.issues });
      }
      solution = await handler.solve(parsed.data, signal);
    }
  }
}
