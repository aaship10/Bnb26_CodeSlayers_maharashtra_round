import { Suspense } from 'react';
import { Link, NavLink, Outlet, ScrollRestoration, useNavigation } from 'react-router-dom';
import { Wordmark } from '@/ui/Logo';
import { ButtonLink, Button } from '@/ui/Button';
import { Spinner } from '@/ui/Spinner';
import { sessionStore, useSession } from '@/state/session';
import { cx } from '@/lib/cx';
import { useSessionKeepAlive } from '@/features/auth/useSessionKeepAlive';

function NavItem({ to, children }: { to: string; children: string }) {
  return (
    <NavLink
      to={to}
      end
      className={({ isActive }) =>
        cx(
          'rounded-md px-3 py-2 font-display text-sm font-semibold hover:bg-paper-3',
          isActive && 'underline decoration-tomato decoration-[3px] underline-offset-[6px]',
        )
      }
    >
      {children}
    </NavLink>
  );
}

export function Layout() {
  const session = useSession();
  const navigation = useNavigation();
  useSessionKeepAlive();

  return (
    <div className="flex min-h-dvh flex-col">
      <a href="#main" className="skip-link">
        Skip to content
      </a>

      <header className="border-b-2 border-ink bg-paper">
        <div className="mx-auto flex w-full max-w-5xl items-center justify-between gap-3 px-4 py-3 sm:px-6">
          <Link to="/" aria-label="Fair Drop, home" className="rounded-md">
            <Wordmark />
          </Link>
          <nav aria-label="Main" className="flex items-center gap-1">
            <NavItem to="/">Drops</NavItem>
            {session ? (
              <Button variant="ghost" size="sm" onClick={() => sessionStore.clear()}>
                Sign out
              </Button>
            ) : (
              <ButtonLink to="/register" variant="secondary" size="sm">
                Sign in
              </ButtonLink>
            )}
          </nav>
        </div>
      </header>

      <main id="main" tabIndex={-1} className="mx-auto w-full max-w-5xl flex-1 px-4 py-8 outline-none sm:px-6 sm:py-12">
        {navigation.state === 'loading' && (
          <div className="mb-4 flex items-center gap-2 text-sm text-ink-3">
            <Spinner className="size-4" label="Loading page" />
            <span aria-hidden="true">Loading…</span>
          </div>
        )}
        <Suspense fallback={<Spinner label="Loading page" />}>
          <Outlet />
        </Suspense>
      </main>

      <footer className="border-t-2 border-ink bg-paper-3">
        <div className="mx-auto flex w-full max-w-5xl flex-col gap-1 px-4 py-6 text-sm text-ink-2 sm:flex-row sm:justify-between sm:px-6">
          <p>Fair Drop, built for Bit N Build.</p>
          <p>Every draw is public and can be checked in your browser.</p>
        </div>
      </footer>
      <ScrollRestoration />
    </div>
  );
}
