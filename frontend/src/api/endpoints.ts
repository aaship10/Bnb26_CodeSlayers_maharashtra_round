import { z } from 'zod';
import { ApiClient, type ChallengeSolution } from './client';
import {
  challengeSchema,
  claimResponseSchema,
  enterResponseSchema,
  eventListSchema,
  eventSchema,
  meSchema,
  sessionResponseSchema,
  statusSchema,
} from './schemas';

const enc = encodeURIComponent;
const nothing = z.unknown();

/** Typed endpoint functions (shared conventions, section 4). Paths carry no /api prefix; the base URL adds it. */
export function createEndpoints(client: ApiClient) {
  return {
    auth: {
      /** hp is the honeypot; real users always send an empty string. */
      register: (body: { email: string; display_name: string; hp: string }, signal?: AbortSignal) =>
        client.request('/auth/register', { method: 'POST', body, schema: nothing, auth: false, signal }).then(() => undefined),
      verify: (body: { email: string; otp: string }, signal?: AbortSignal) =>
        client.request('/auth/verify', { method: 'POST', body, schema: sessionResponseSchema, auth: false, signal }),
      refresh: (signal?: AbortSignal) =>
        client.request('/auth/refresh', { method: 'POST', schema: sessionResponseSchema, signal }),
      me: (signal?: AbortSignal) => client.request('/auth/me', { schema: meSchema, signal }),
    },
    defence: {
      challenge: (eventId: string, signal?: AbortSignal) =>
        client.request('/defence/challenge', { method: 'POST', body: { event_id: eventId }, schema: challengeSchema, signal }),
    },
    events: {
      list: (signal?: AbortSignal) => client.request('/events', { schema: eventListSchema, auth: false, signal }),
      get: (id: string, signal?: AbortSignal) => client.request(`/events/${enc(id)}`, { schema: eventSchema, auth: false, signal }),
      /** Idempotent: safe to retry automatically. */
      enter: (id: string, challenge?: ChallengeSolution, signal?: AbortSignal) =>
        client.request(`/events/${enc(id)}/enter`, { method: 'POST', schema: enterResponseSchema, challenge, signal }),
      status: (id: string, signal?: AbortSignal) => client.request(`/events/${enc(id)}/status`, { schema: statusSchema, signal }),
      /** Always pass the persisted key from getClaimKey(); retries must reuse it. */
      claim: (id: string, idempotencyKey: string, challenge?: ChallengeSolution, signal?: AbortSignal) =>
        client.request(`/events/${enc(id)}/claim`, {
          method: 'POST',
          schema: claimResponseSchema,
          idempotencyKey,
          challenge,
          signal,
        }),
    },
  };
}

export type Api = ReturnType<typeof createEndpoints>;
