import { StrictMode, Suspense, lazy } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider } from 'react-router-dom';
import '@fontsource-variable/bricolage-grotesque';
import '@fontsource-variable/dm-sans';
import '@fontsource-variable/jetbrains-mono';
import './styles/index.css';
import { prefetchAttendeeRoutes, router } from './app/router';
import { AnnouncerProvider } from './app/Announcer';
import { createQueryClient } from './api/queryClient';

const queryClient = createQueryClient();

// Dev-only mock controls. `import.meta.env.DEV` is a build-time constant, so this
// whole branch (and the dev panel chunk) is dropped from production builds.
const DevPanel = import.meta.env.DEV ? lazy(() => import('./dev/DevPanel')) : null;

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AnnouncerProvider>
        <RouterProvider router={router} />
        {DevPanel && (
          <Suspense fallback={null}>
            <DevPanel />
          </Suspense>
        )}
      </AnnouncerProvider>
    </QueryClientProvider>
  </StrictMode>,
);

prefetchAttendeeRoutes();
