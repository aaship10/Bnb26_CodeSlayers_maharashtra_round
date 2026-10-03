import { useEffect, useRef } from 'react';
import type { ChallengeUiState } from './useChallenge';
import { CaptchaWidget } from './CaptchaWidget';
import { Spinner } from '@/ui/Spinner';

/** Expected attempts to find a PoW solution is 2^bits (geometric), so progress is an estimate, never a promise. */
export function powProgress(hashes: number, difficultyBits: number): number {
  const expected = 2 ** difficultyBits;
  // Cap below 100%: the real answer can take longer than average, and a bar stuck at 100% reads as a hang.
  return Math.min(0.95, hashes / expected);
}

interface Props {
  state: ChallengeUiState;
  onCaptcha: (token: string) => void;
  onCancel?: () => void;
}

/**
 * "Verifying you're human…": shown inline in place of the action it interrupted.
 * The proof-of-work variant needs no interaction; CAPTCHA asks for one click.
 */
export function ChallengePanel({ state, onCaptcha, onCancel }: Props) {
  const headingRef = useRef<HTMLHeadingElement>(null);

  // A CAPTCHA needs the user; move focus to it. PoW stays out of the way.
  useEffect(() => {
    if (state.kind === 'captcha') headingRef.current?.focus();
  }, [state.kind]);

  if (state.kind === 'idle') return null;

  return (
    <div className="rounded-md border-2 border-ink bg-cobalt-tint p-4" role="status" aria-live="polite">
      <h3 ref={headingRef} tabIndex={-1} className="flex items-center gap-2 font-display text-lg outline-none">
        {state.kind === 'pow' && <Spinner className="size-5" />}
        Verifying you’re human…
      </h3>

      {state.kind === 'pow' && (
        <>
          <p className="mt-1 text-sm text-ink-2">Your device is doing a small calculation. It takes a few seconds and needs nothing from you.</p>
          <div
            className="mt-3 h-3 overflow-hidden rounded-full border-2 border-ink bg-paper-2"
            role="progressbar"
            aria-label="Verification progress (estimate)"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(powProgress(state.hashes, state.difficultyBits) * 100)}
          >
            <div
              className="h-full bg-tomato transition-[width] duration-150 motion-reduce:transition-none"
              style={{ width: `${Math.max(4, powProgress(state.hashes, state.difficultyBits) * 100)}%` }}
            />
          </div>
        </>
      )}

      {state.kind === 'captcha' && (
        <div className="mt-3">
          <p className="mb-3 text-sm text-ink-2">One quick check, then we will finish what you started.</p>
          <CaptchaWidget challenge={state.challenge} onSolved={onCaptcha} />
        </div>
      )}

      {onCancel && (
        <button type="button" onClick={onCancel} className="mt-3 text-sm font-semibold underline underline-offset-4 hover:no-underline">
          Cancel
        </button>
      )}
    </div>
  );
}
