import { createBrowserRouter, type RouteObject } from 'react-router-dom';
import { Layout } from './Layout';
import { HomePage } from '@/pages/HomePage';
import { NotFoundPage } from '@/pages/NotFoundPage';

/**
 * Route table. Only the home page is in the initial bundle; every other screen
 * loads on demand. The attendee screens are prefetched while the browser is
 * idle (see prefetchAttendeeRoutes), so moving between them stays instant.
 */
const attendee = {
  register: () => import('@/features/auth/RegisterPage'),
  event: () => import('@/features/event/EventPage'),
  status: () => import('@/features/status/StatusPage'),
  claim: () => import('@/features/claim/ClaimPage'),
  ticket: () => import('@/features/ticket/TicketPage'),
};

const routes: RouteObject[] = [
  {
    element: <Layout />,
    children: [
      { index: true, element: <HomePage /> },

      // attendee
      { path: 'register', lazy: async () => ({ Component: (await attendee.register()).RegisterPage }) },
      { path: 'events/:id', lazy: async () => ({ Component: (await attendee.event()).EventPage }) },
      { path: 'events/:id/status', lazy: async () => ({ Component: (await attendee.status()).StatusPage }) },
      { path: 'events/:id/claim', lazy: async () => ({ Component: (await attendee.claim()).ClaimPage }) },
      { path: 'events/:id/ticket', lazy: async () => ({ Component: (await attendee.ticket()).TicketPage }) },

      // public fairness
      { path: 'events/:id/fairness', lazy: () => import('@/features/fairness/FairnessPage') },
      { path: 'events/:id/audit', lazy: () => import('@/features/fairness/AuditPage') },

      // organizer and simulator: not linked from the attendee nav
      { path: 'admin', lazy: () => import('@/features/admin/AdminHome') },
      { path: 'admin/events/:id', lazy: () => import('@/features/admin/AdminEventPage') },
      { path: 'admin/sim', lazy: () => import('@/features/sim/SimPanel') },
      { path: 'admin/sim/runs/:id', lazy: () => import('@/features/sim/RunPage') },
      { path: 'admin/sim/compare', lazy: () => import('@/features/sim/ComparePage') },
      { path: 'admin/sim/experiments', lazy: () => import('@/features/sim/ExperimentsPage') },

      ...(import.meta.env.DEV
        ? ([
            { path: '__dev/styleguide', lazy: async () => ({ Component: (await import('@/dev/Styleguide')).Styleguide }) },
            { path: '__dev/pow-bench', lazy: async () => ({ Component: (await import('@/dev/PowBench')).PowBench }) },
          ] satisfies RouteObject[])
        : []),

      { path: '*', element: <NotFoundPage /> },
    ],
  },
];

export const router = createBrowserRouter(routes);

/**
 * Warm the attendee chunks once the page is idle, so tapping "View drop" or
 * "Enter" never waits on a download. Low priority, and skipped when the
 * connection asks to save data.
 */
export function prefetchAttendeeRoutes(): void {
  const conn = (navigator as Navigator & { connection?: { saveData?: boolean } }).connection;
  if (conn?.saveData) return;
  const run = () => {
    for (const load of Object.values(attendee)) void load().catch(() => undefined);
  };
  const ric = (window as Window & { requestIdleCallback?: (cb: () => void, o?: { timeout: number }) => number }).requestIdleCallback;
  if (ric) ric(run, { timeout: 4000 });
  else setTimeout(run, 2000);
}
