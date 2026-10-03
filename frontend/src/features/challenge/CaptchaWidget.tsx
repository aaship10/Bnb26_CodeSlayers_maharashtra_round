import { useState } from 'react';
import { Check, ShieldCheck } from 'lucide-react';
import type { Challenge } from '@/api/schemas';
import { Alert } from '@/ui/Alert';
import { Spinner } from '@/ui/Spinner';
import { cx } from '@/lib/cx';

/** What the mock server accepts as a CAPTCHA solution. Real providers hand back their own token. */
export const MOCK_CAPTCHA_TOKEN = 'mock-captcha-ok';

interface Props {
  challenge: Challenge;
  onSolved: (token: string) => void;
}

/** Stand-in for a hosted CAPTCHA: one labelled checkbox-style control, fully keyboard operable. */
function MockCaptcha({ onSolved }: { onSolved: (token: string) => void }) {
  const [checking, setChecking] = useState(false);
  const [done, setDone] = useState(false);

  const verify = () => {
    if (checking || done) return;
    setChecking(true);
    // A real widget would run its own risk check here.
    setTimeout(() => {
      setChecking(false);
      setDone(true);
      onSolved(MOCK_CAPTCHA_TOKEN);
    }, 700);
  };

  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={done}
      onClick={verify}
      disabled={checking || done}
      className={cx(
        'flex min-h-14 w-full max-w-sm items-center gap-3 rounded-md border-2 border-ink bg-paper-2 px-4 text-left font-display text-base font-semibold',
        'shadow-pop-sm transition-colors hover:bg-sun-tint disabled:cursor-default disabled:hover:bg-paper-2',
      )}
    >
      <span className="grid size-7 shrink-0 place-items-center rounded-sm border-2 border-ink bg-white">
        {checking ? <Spinner className="size-4" /> : done ? <Check className="size-5 text-pine" aria-hidden="true" /> : null}
      </span>
      <span>{done ? 'Thanks, you’re verified' : checking ? 'Checking…' : 'I’m a person, not a bot'}</span>
      <ShieldCheck className="ml-auto size-5 text-ink-3" aria-hidden="true" />
    </button>
  );
}

/**
 * Renders the CAPTCHA the server asked for. Only the mock provider exists today;
 * real providers (hCaptcha, Turnstile, reCAPTCHA) are wired during integration
 * once B confirms which one the stack uses. An unknown provider fails visibly.
 */
export function CaptchaWidget({ challenge, onSolved }: Props) {
  const provider = challenge.captcha?.provider;
  return (
    <div className="space-y-3">
      {provider === 'mock' ? (
        <MockCaptcha onSolved={onSolved} />
      ) : (
        <Alert tone="error" title="This check isn’t available here">
          The verification provider “{provider}” isn’t set up in this build.
        </Alert>
      )}
      <p className="max-w-prose text-sm text-ink-3">
        Can’t use this check, for example with a screen reader? Tell the organisers and they will help you enter another way.
      </p>
    </div>
  );
}
