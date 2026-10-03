import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { MailCheck } from 'lucide-react';
import { api, describeError, isApiError } from '@/api';
import { useAnnounce } from '@/app/Announcer';
import { sessionStore, useSession } from '@/state/session';
import { safeInternalPath } from '@/lib/safePath';
import { useDocumentTitle } from '@/lib/useDocumentTitle';
import { Alert } from '@/ui/Alert';
import { Ball } from '@/ui/Ball';
import { Button, ButtonLink } from '@/ui/Button';
import { Field } from '@/ui/Field';
import { Ticket } from '@/ui/Ticket';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const RESEND_COOLDOWN_S = 30;

type Step = 'details' | 'code';

/**
 * Honeypot field. Humans never see or reach it (visually hidden, aria-hidden,
 * tabindex -1, autocomplete off); bots that fill every input reveal themselves.
 * Always sent, empty for real people.
 */
function Honeypot({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div aria-hidden="true" style={{ position: 'absolute', left: '-10000px', width: 1, height: 1, overflow: 'hidden' }}>
      <label>
        Leave this field empty
        <input name="hp" type="text" tabIndex={-1} autoComplete="off" value={value} onChange={(e) => onChange(e.target.value)} />
      </label>
    </div>
  );
}

export function RegisterPage() {
  useDocumentTitle('Sign in');
  const session = useSession();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const announce = useAnnounce();
  const next = safeInternalPath((location.state as { from?: unknown } | null)?.from);

  const [step, setStep] = useState<Step>('details');
  const [displayName, setDisplayName] = useState('');
  const [email, setEmail] = useState('');
  const [hp, setHp] = useState('');
  const [otp, setOtp] = useState('');
  const [fieldErrors, setFieldErrors] = useState<{ displayName?: string; email?: string }>({});
  const [cooldown, setCooldown] = useState(0);
  const otpRef = useRef<HTMLInputElement>(null);

  const register = useMutation({
    mutationFn: () => api.auth.register({ email: email.trim(), display_name: displayName.trim(), hp }),
    onSuccess: () => {
      setStep('code');
      setOtp('');
      setCooldown(RESEND_COOLDOWN_S);
      announce(`We sent a code to ${email.trim()}`);
    },
    onError: (e) => {
      // A 429 tells us exactly how long to wait; show it on the resend button too.
      if (isApiError(e) && e.code === 'RATE_LIMITED' && e.retryAfterMs) setCooldown(Math.ceil(e.retryAfterMs / 1000));
    },
  });

  const verify = useMutation({
    mutationFn: () => api.auth.verify({ email: email.trim(), otp: otp.trim() }),
    onSuccess: (res) => {
      sessionStore.set({ token: res.token, expires_at: res.expires_at, user_id: res.user_id });
      void queryClient.invalidateQueries();
      announce('You are signed in');
      navigate(next, { replace: true });
    },
  });

  // Resend cooldown is a display countdown for a duration, so the local clock is fine.
  useEffect(() => {
    if (cooldown <= 0) return;
    const t = setTimeout(() => setCooldown((c) => c - 1), 1000);
    return () => clearTimeout(t);
  }, [cooldown]);

  useEffect(() => {
    if (step === 'code') otpRef.current?.focus();
  }, [step]);

  const submitDetails = (e: FormEvent) => {
    e.preventDefault();
    if (register.isPending) return;
    const errs: typeof fieldErrors = {};
    if (!displayName.trim()) errs.displayName = 'Tell us what to call you.';
    else if (displayName.trim().length > 60) errs.displayName = 'Please keep it under 60 characters.';
    if (!EMAIL_RE.test(email.trim())) errs.email = 'That doesn’t look like an email address.';
    setFieldErrors(errs);
    if (Object.keys(errs).length > 0) return;
    register.mutate();
  };

  const submitCode = (e: FormEvent) => {
    e.preventDefault();
    if (verify.isPending || otp.trim().length === 0) return;
    verify.mutate();
  };

  const resend = () => {
    if (cooldown > 0 || register.isPending) return;
    register.mutate();
  };

  if (session) {
    return (
      <div className="mx-auto max-w-xl">
        <Alert tone="success" title="You’re signed in" action={<ButtonLink to={next} size="sm">Carry on</ButtonLink>}>
          Nothing more to do here.
        </Alert>
      </div>
    );
  }

  const registerError = register.error ? describeError(register.error) : null;
  const verifyError = verify.error ? describeError(verify.error) : null;

  return (
    <div className="mx-auto grid max-w-4xl items-start gap-10 md:grid-cols-[1fr_1.1fr]">
      <div className="space-y-5 md:pt-4">
        <Ball n={1} color="sun" className="size-16" />
        <h1 className="font-display text-3xl sm:text-4xl">One person, one place in the drum.</h1>
        <p className="text-lg text-ink-2">
          We ask for an email so each person can enter once. No password: we send a short code, you type it in, and you’re ready.
        </p>
        <p className="text-sm text-ink-3">You only do this once on this device.</p>
      </div>

      <Ticket
        stub={
          step === 'details' ? (
            <span className="text-sm text-ink-2">Step 1 of 2: your details</span>
          ) : (
            <span className="text-sm text-ink-2">Step 2 of 2: check your inbox</span>
          )
        }
      >
        {step === 'details' ? (
          <form onSubmit={submitDetails} noValidate className="relative space-y-5" aria-label="Sign up">
            <h2 className="font-display text-2xl">Get your code</h2>
            <Field
              label="What should we call you?"
              name="display_name"
              autoComplete="name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              error={fieldErrors.displayName}
              maxLength={80}
            />
            <Field
              label="Email"
              name="email"
              type="email"
              autoComplete="email"
              inputMode="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              error={fieldErrors.email}
              hint="We’ll send a one-time code. We don’t send marketing."
            />
            <Honeypot value={hp} onChange={setHp} />

            {registerError && (
              <Alert tone={registerError.tone} title={registerError.title}>
                {registerError.detail ?? registerError.body}
              </Alert>
            )}

            <Button type="submit" size="lg" className="w-full" loading={register.isPending}>
              {register.isPending ? 'Sending…' : 'Send me a code'}
            </Button>
          </form>
        ) : (
          <form onSubmit={submitCode} noValidate className="space-y-5" aria-label="Enter your code">
            <div className="flex items-start gap-3">
              <MailCheck className="mt-1 size-6 shrink-0 text-pine" aria-hidden="true" />
              <div>
                <h2 className="font-display text-2xl">Check your email</h2>
                <p className="mt-1 text-ink-2">
                  We sent a code to <b className="break-all text-ink">{email.trim()}</b>.
                </p>
              </div>
            </div>

            <Field
              ref={otpRef}
              label="One-time code"
              name="otp"
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="[0-9]*"
              maxLength={8}
              value={otp}
              onChange={(e) => setOtp(e.target.value.replace(/\s+/g, ''))}
              className="[&_input]:text-center [&_input]:font-mono [&_input]:text-2xl [&_input]:tracking-[0.4em]"
              error={verifyError?.code === 'VALIDATION_ERROR' ? (verifyError.detail ?? 'That code didn’t work.') : undefined}
            />

            {verifyError && verifyError.code !== 'VALIDATION_ERROR' && (
              <Alert tone={verifyError.tone} title={verifyError.title}>
                {verifyError.body}
              </Alert>
            )}
            {registerError && (
              <Alert tone={registerError.tone} title={registerError.title}>
                {registerError.body}
              </Alert>
            )}

            <Button type="submit" size="lg" className="w-full" loading={verify.isPending} disabled={otp.trim().length === 0}>
              {verify.isPending ? 'Checking…' : 'Confirm and continue'}
            </Button>

            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <Button type="button" variant="ghost" size="sm" onClick={resend} disabled={cooldown > 0 || register.isPending}>
                {cooldown > 0 ? `Resend code in ${cooldown}s` : 'Resend code'}
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => {
                  setStep('details');
                  register.reset();
                  verify.reset();
                }}
              >
                Use a different email
              </Button>
            </div>
          </form>
        )}
      </Ticket>

      <p className="text-center text-sm text-ink-3 md:col-span-2">
        <Link to="/" className="link">
          Back to the drops
        </Link>
      </p>
    </div>
  );
}
