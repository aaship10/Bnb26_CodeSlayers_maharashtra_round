import { ApiClient } from './client';
import { createEndpoints } from './endpoints';
import { serverClock } from '@/lib/serverClock';
import { getDeviceId } from '@/lib/storage';
import { sessionStore } from '@/state/session';

/** Where the SPA reaches the backend. nginx (prod) and Vite (dev) both map /api -> backend, stripping the prefix. */
export const API_BASE = '/api';

export const apiClient = new ApiClient({
  baseUrl: API_BASE,
  getToken: () => sessionStore.getSnapshot()?.token ?? null,
  getDeviceId,
  clock: serverClock,
});

export const api = createEndpoints(apiClient);

export * from './errors';
export * from './schemas';
