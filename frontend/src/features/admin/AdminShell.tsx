import { useRef, useState, type FormEvent, type ReactNode } from 'react';
import { NavLink } from 'react-router-dom';
import { KeyRound, Lock } from 'lucide-react';
import { describeError, isApiError } from '@/api/errors';
import { cx } from '@/lib/cx';
import { useDocumentTitle } from '@/lib/useDocumentTitle';
import { Alert } from '@/ui/Alert';
import { Button, ButtonLink } from '@/ui/Button';
import { Dialog } from '@/ui/Dialog';
import { Field } from '@/ui/Field';
import { adminApi } from './adminApi';
import { adminToken, useAdminToken } from './adminToken';

/** Asks for the admin token in a modal. Verifies it with the server before keeping it (sessionStorage only). */
function UnlockDialog() {
  const [value, setValue] = useState('');
  const [error, setError] = useState<string | null>(adminToken.reason);
  const [pending, setPending] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const token = value.trim();
    if (!token || pending) return;
    setPending(true);
    setError(null);
    try {
      await adminApi.verifyToken(token);
      adminToken.set(token);
    } catch (err) {
      setError(
        isApiError(err) && (err.code === 'UNAUTHENTICATED' || err.code === 'FORBIDDEN')
          ? 'That token wasn’t accepted. Check it and try again.'
          : describeError(err).title,
      );
      inputRef.current?.focus();
    } finally {
      setPending(false);
    }
  };

  return (
    <Dialog open title="Organizer access" initialFocus={inputRef}>
      <form onSubmit={submit} className="space-y-4">
        <p className="text-ink-2">Enter the admin token to manage events. It stays in this tab only and is forgotten when you close it.</p>
        <Field
          ref={inputRef}
          label="Admin token"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          error={error ?? undefined}
        />
        <div className="flex items-center justify-between gap-3">
          <ButtonLink to="/" variant="ghost" size="sm">
            Back to the drops
          </ButtonLink>
          <Button type="submit" loading={pending} disabled={!value.trim()} leading={<KeyRound className="size-5" aria-hidden="true" />}>
            Unlock
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function Tab({ to, children, end }: { to: string; children: string; end?: boolean }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        cx('rounded-md px-3 py-1.5 text-sm font-semibold', isActive ? 'bg-ink text-paper' : 'text-ink-2 hover:bg-paper-3 hover:text-ink')
      }
    >
      {children}
    </NavLink>
  );
}

/**
 * Frame for every organizer page: a quiet tool bar and the token gate.
 * Nothing behind it renders (or fetches) until a verified token exists.
 */
export function AdminShell({ title, children }: { title: string; children: ReactNode }) {
  const token = useAdminToken();
  useDocumentTitle(`Organizer · ${title}`);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border-2 border-ink bg-paper-3 px-3 py-2">
        <div className="flex items-center gap-2">
          <span className="px-2 font-mono text-xs font-bold uppercase tracking-widest text-ink-3">Organizer</span>
          <nav aria-label="Organizer" className="flex gap-1">
            <Tab to="/admin" end>
              Events
            </Tab>
            <Tab to="/admin/sim">Simulator</Tab>
          </nav>
        </div>
        {token && (
          <Button variant="ghost" size="sm" leading={<Lock className="size-4" aria-hidden="true" />} onClick={() => adminToken.clear()}>
            Lock
          </Button>
        )}
      </div>

      {token ? (
        children
      ) : (
        <>
          <Alert title="Locked">Organizer tools need the admin token.</Alert>
          <UnlockDialog />
        </>
      )}
    </div>
  );
}
