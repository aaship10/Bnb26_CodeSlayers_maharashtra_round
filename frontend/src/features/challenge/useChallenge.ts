import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Challenge } from '@/api/schemas';
import type { ChallengeHandler } from './withChallenge';
import { solvePowInWorker } from './solvePow';

export type ChallengeUiState =
  | { kind: 'idle' }
  | { kind: 'pow'; difficultyBits: number; hashes: number }
  | { kind: 'captcha'; challenge: Challenge };

export interface UseChallenge {
  state: ChallengeUiState;
  handler: ChallengeHandler;
  /** Called by the CAPTCHA widget with the provider token. */
  submitCaptcha: (token: string) => void;
}

/** Throttle React state updates from the worker's progress stream. */
const PROGRESS_INTERVAL_MS = 120;

/**
 * Bridges the pure challenge flow (withChallenges) to UI state. The handler is
 * stable across renders; aborting its signal (navigation, unmount, cancel)
 * terminates the PoW worker and rejects any pending CAPTCHA.
 */
export function useChallenge(): UseChallenge {
  const [state, setState] = useState<ChallengeUiState>({ kind: 'idle' });
  const captchaResolve = useRef<((token: string) => void) | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const set = useCallback((next: ChallengeUiState) => {
    if (mounted.current) setState(next);
  }, []);

  const handler = useMemo<ChallengeHandler>(
    () => ({
      async solve(challenge, signal) {
        try {
          if (challenge.type === 'pow' && challenge.pow) {
            const { prefix, difficulty_bits } = challenge.pow;
            set({ kind: 'pow', difficultyBits: difficulty_bits, hashes: 0 });
            let last = 0;
            const nonce = await solvePowInWorker(
              { prefix, difficultyBits: difficulty_bits },
              {
                signal,
                onProgress: (hashes) => {
                  const now = performance.now();
                  if (now - last < PROGRESS_INTERVAL_MS) return;
                  last = now;
                  set({ kind: 'pow', difficultyBits: difficulty_bits, hashes });
                },
              },
            );
            return { id: challenge.id, solution: nonce };
          }

          if (challenge.type === 'captcha') {
            set({ kind: 'captcha', challenge });
            const token = await new Promise<string>((resolve, reject) => {
              captchaResolve.current = resolve;
              signal.addEventListener('abort', () => reject(new DOMException('cancelled', 'AbortError')), { once: true });
            });
            return { id: challenge.id, solution: token };
          }

          throw new Error(`Unsupported challenge type: ${challenge.type}`);
        } finally {
          captchaResolve.current = null;
          set({ kind: 'idle' });
        }
      },
    }),
    [set],
  );

  const submitCaptcha = useCallback((token: string) => captchaResolve.current?.(token), []);

  return { state, handler, submitCaptcha };
}
