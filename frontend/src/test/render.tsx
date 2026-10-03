import type { ReactElement } from 'react';
import { render } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { AnnouncerProvider } from '@/app/Announcer';

export function makeQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 }, mutations: { retry: false } } });
}

/** Shows where the router currently is, so tests can assert navigation. */
export function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location">{loc.pathname}</div>;
}

interface Options {
  path?: string;
  route?: string;
  state?: unknown;
}

/** Render a screen inside the providers it needs (query client, router, announcer). */
export function renderRoute(ui: ReactElement, { path = '/', route = '/', state }: Options = {}) {
  const client = makeQueryClient();
  const utils = render(
    <QueryClientProvider client={client}>
      <AnnouncerProvider>
        <MemoryRouter initialEntries={[{ pathname: path, state }]}>
          <Routes>
            <Route path={route} element={ui} />
            <Route path="*" element={null} />
          </Routes>
          <LocationProbe />
        </MemoryRouter>
      </AnnouncerProvider>
    </QueryClientProvider>,
  );
  return { ...utils, client };
}

export function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}
