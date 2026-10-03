import { createBrowserRouter, type RouteObject } from 'react-router-dom';
import { Layout } from './Layout';
import { HomePage } from '@/pages/HomePage';
import { NotFoundPage } from '@/pages/NotFoundPage';
import { RegisterPage } from '@/features/auth/RegisterPage';
import { EventPage } from '@/features/event/EventPage';
import { StatusPage } from '@/features/status/StatusPage';
import { ClaimPage } from '@/features/claim/ClaimPage';
import { TicketPage } from '@/features/ticket/TicketPage';

/**
 * Route table. Attendee screens are in the main bundle; organizer, fairness,
 * simulator and dev routes are code-split so the attendee path stays light.
 * Stubs (ComingSoon) are swapped for real screens stage by stage.
 */
const routes: RouteObject[] = [
  {
    element: <Layout />,
    children: [
      { index: true, element: <HomePage /> },

      // attendee
      { path: 'register', element: <RegisterPage /> },
      { path: 'events/:id', element: <EventPage /> },
      { path: 'events/:id/status', element: <StatusPage /> },
      { path: 'events/:id/claim', element: <ClaimPage /> },
      { path: 'events/:id/ticket', element: <TicketPage /> },

      // public fairness (stage 5)
      { path: 'events/:id/fairness', lazy: async () => ({ Component: (await import('@/features/fairness/pages')).FairnessPage }) },
      { path: 'events/:id/audit', lazy: async () => ({ Component: (await import('@/features/fairness/pages')).AuditPage }) },

      // organizer (stage 4) and simulator / results (stage 6): not linked from the attendee nav
      { path: 'admin', lazy: () => import('@/features/admin/AdminHome') },
      { path: 'admin/events/:id', lazy: () => import('@/features/admin/AdminEventPage') },
      { path: 'admin/sim', lazy: async () => ({ Component: (await import('@/features/sim/pages')).SimPanel }) },
      { path: 'admin/sim/runs/:id', lazy: async () => ({ Component: (await import('@/features/sim/pages')).RunResults }) },
      { path: 'admin/sim/compare', lazy: async () => ({ Component: (await import('@/features/sim/pages')).Compare }) },
      { path: 'admin/sim/experiments', lazy: async () => ({ Component: (await import('@/features/sim/pages')).Experiments }) },

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
