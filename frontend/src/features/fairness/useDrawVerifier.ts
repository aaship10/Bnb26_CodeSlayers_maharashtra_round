import { useCallback, useEffect, useRef, useState } from 'react';
import { API_BASE, apiClient } from '@/api';
import { getDeviceId } from '@/lib/storage';
import { bestCrypto } from './cryptoImpl';
import { entrantsSchema, resultsSchema, type Fairness } from './schemas';
import { verifyDraw, type Step, type Verdict, type VerifyOutcome } from './verifyDraw';
import type { VerifyMessage, VerifyRequest } from './verify.worker';

export interface VerifierState {
  running: boolean;
  steps: Step[];
  progress: number;
  verdict: Verdict | null;
  order: string[] | null;
  winnersCount: number | null;
  engine: string | null;
  error: string | null;
  ms: number | null;
}

const initial: VerifierState = { running: false, steps: [], progress: 0, verdict: null, order: null, winnersCount: null, engine: null, error: null, ms: null };

/**
 * Drives the verifier worker. One run at a time; cancel() or leaving the page
 * terminates the worker. Without Worker support the same code runs on the main
 * thread (slower to feel, same result).
 */
export function useDrawVerifier(eventId: string) {
  const [state, setState] = useState<VerifierState>(initial);
  const worker = useRef<Worker | null>(null);
  const started = useRef(0);

  const stop = useCallback(() => {
    worker.current?.terminate();
    worker.current = null;
  }, []);

  useEffect(() => stop, [stop]);

  const finish = useCallback((outcome: VerifyOutcome, engine: string) => {
    setState((s) => ({
      ...s,
      running: false,
      steps: outcome.steps,
      verdict: outcome.verdict,
      order: outcome.order ?? null,
      winnersCount: outcome.winnersCount ?? null,
      engine,
      progress: 1,
      ms: Math.round(performance.now() - started.current),
    }));
  }, []);

  const run = useCallback(
    (fairness: Fairness) => {
      stop();
      started.current = performance.now();
      setState({ ...initial, running: true });

      if (typeof Worker === 'undefined') {
        const crypto = bestCrypto();
        void verifyDraw(
          {
            eventId,
            fairness,
            loadEntrants: async () =>
              (await apiClient.request(`/events/${encodeURIComponent(eventId)}/fairness/entrants`, { schema: entrantsSchema, auth: false, timeoutMs: 60_000 })).entrants,
            loadResults: () =>
              apiClient.request(`/events/${encodeURIComponent(eventId)}/fairness/results`, { schema: resultsSchema, auth: false, timeoutMs: 60_000 }).catch(() => null),
          },
          crypto,
          { onSteps: (steps) => setState((s) => ({ ...s, steps })), onProgress: (progress) => setState((s) => ({ ...s, progress })) },
        )
          .then((o) => finish(o, crypto.name))
          .catch((e: unknown) => setState((s) => ({ ...s, running: false, error: String(e) })));
        return;
      }

      const w = new Worker(new URL('./verify.worker.ts', import.meta.url), { type: 'module' });
      worker.current = w;
      w.onmessage = (e: MessageEvent<VerifyMessage>) => {
        const m = e.data;
        if (m.type === 'steps') setState((s) => ({ ...s, steps: m.steps }));
        else if (m.type === 'progress') setState((s) => ({ ...s, progress: m.fraction }));
        else if (m.type === 'done') {
          finish(m.outcome, m.engine);
          stop();
        } else {
          setState((s) => ({ ...s, running: false, error: m.message }));
          stop();
        }
      };
      w.onerror = (e) => {
        setState((s) => ({ ...s, running: false, error: e.message || 'The verifier stopped unexpectedly.' }));
        stop();
      };
      w.postMessage({ eventId, apiBase: API_BASE, deviceId: getDeviceId(), fairness } satisfies VerifyRequest);
    },
    [eventId, finish, stop],
  );

  const cancel = useCallback(() => {
    stop();
    setState((s) => ({ ...s, running: false }));
  }, [stop]);

  return { ...state, run, cancel };
}
