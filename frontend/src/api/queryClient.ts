import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query';
import { queryRetryDelay, shouldRetryQuery } from './retry';
import { isApiError } from './errors';
import { sessionStore } from '@/state/session';

/**
 * Any authenticated call that comes back UNAUTHENTICATED means the stored token is
 * dead (expired, server reset). Forget it so the UI offers "Sign in" instead of
 * failing every request in the same way.
 */
function dropSessionIfExpired(error: unknown): void {
  if (isApiError(error) && error.code === 'UNAUTHENTICATED' && sessionStore.getSnapshot()) sessionStore.clear();
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({ onError: dropSessionIfExpired }),
    mutationCache: new MutationCache({ onError: dropSessionIfExpired }),
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        gcTime: 5 * 60_000,
        retry: shouldRetryQuery,
        retryDelay: queryRetryDelay,
        // Focus refetch would make every returning tab hit the API at once.
        // Timer/SSE driven refreshes carry their own jitter instead.
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
      },
      mutations: { retry: false },
    },
  });
}

export const queryKeys = {
  events: ['events'] as const,
  event: (id: string) => ['events', id] as const,
  status: (id: string) => ['events', id, 'status'] as const,
  me: ['me'] as const,
};
