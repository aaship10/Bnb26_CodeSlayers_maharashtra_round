import { z } from 'zod';
import { apiClient } from '@/api';
import { isApiError } from '@/api/errors';
import { adminToken } from './adminToken';
import {
  adminEventListSchema,
  adminEventSchema,
  invariantsSchema,
  presetListSchema,
  statsSchema,
  transitionResponseSchema,
  type CreateEventBody,
  type DefenceConfig,
  type LifecycleAction,
} from './schemas';

const enc = encodeURIComponent;

/**
 * Organizer endpoints. They send X-Admin-Token and deliberately NOT the attendee's
 * bearer token, so admin actions never ride on someone's personal session.
 * A 401/403 means the token is wrong or revoked: forget it and ask again.
 */
async function call<T>(path: string, schema: z.ZodType<T>, init: { method?: 'GET' | 'POST' | 'PATCH'; body?: unknown; idempotencyKey?: string; signal?: AbortSignal; token?: string } = {}): Promise<T> {
  const token = init.token ?? adminToken.get() ?? '';
  try {
    return await apiClient.request(path, {
      schema,
      method: init.method,
      body: init.body,
      idempotencyKey: init.idempotencyKey,
      signal: init.signal,
      auth: false,
      headers: { 'X-Admin-Token': token },
    });
  } catch (e) {
    if (!init.token && isApiError(e) && (e.code === 'UNAUTHENTICATED' || e.code === 'FORBIDDEN')) {
      adminToken.clear('Your admin token was not accepted. Enter it again.');
    }
    throw e;
  }
}

export const adminApi = {
  /** Check a candidate token without storing it. */
  verifyToken: (token: string, signal?: AbortSignal) => call('/admin/defence/presets', presetListSchema, { token, signal }),
  presets: (signal?: AbortSignal) => call('/admin/defence/presets', presetListSchema, { signal }),
  events: (signal?: AbortSignal) => call('/admin/events', adminEventListSchema, { signal }),
  event: (id: string, signal?: AbortSignal) => call(`/admin/events/${enc(id)}`, adminEventSchema, { signal }),
  create: (body: CreateEventBody, idempotencyKey: string) =>
    call('/admin/events', adminEventSchema, { method: 'POST', body, idempotencyKey }),
  transition: (id: string, action: LifecycleAction, idempotencyKey: string) =>
    call(`/admin/events/${enc(id)}/${action}`, transitionResponseSchema, { method: 'POST', idempotencyKey }),
  patchConfig: (id: string, defences: DefenceConfig, idempotencyKey: string) =>
    call(`/admin/events/${enc(id)}/config`, adminEventSchema, { method: 'PATCH', body: { defences }, idempotencyKey }),
  stats: (id: string, signal?: AbortSignal) => call(`/admin/events/${enc(id)}/stats`, statsSchema, { signal }),
  invariants: (id: string, signal?: AbortSignal) => call(`/admin/events/${enc(id)}/invariants`, invariantsSchema, { signal }),
  reset: (id: string, idempotencyKey: string) => call(`/admin/events/${enc(id)}/reset`, transitionResponseSchema, { method: 'POST', idempotencyKey }),
};

export const adminKeys = {
  all: ['admin'] as const,
  presets: ['admin', 'presets'] as const,
  events: ['admin', 'events'] as const,
  event: (id: string) => ['admin', 'events', id] as const,
  stats: (id: string) => ['admin', 'events', id, 'stats'] as const,
  invariants: (id: string) => ['admin', 'events', id, 'invariants'] as const,
};
