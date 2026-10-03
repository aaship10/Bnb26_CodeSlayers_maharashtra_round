import { createChunkedSolver } from './pow';
import type { PowMessage, PowRequest } from './pow.worker';

export interface SolveParams {
  prefix: string;
  difficultyBits: number;
}

export interface SolveHooks {
  /** Aborting terminates the worker and rejects with an AbortError. */
  signal?: AbortSignal;
  onProgress?: (hashes: number) => void;
}

function abortError(): DOMException {
  return new DOMException('Proof-of-work cancelled', 'AbortError');
}

/**
 * Solve a PoW challenge off the main thread. Falls back to a time-sliced loop on
 * the main thread when Web Workers are unavailable or blocked (strict CSP,
 * very old browsers) so entering still works, just less smoothly.
 */
export function solvePowInWorker(params: SolveParams, hooks: SolveHooks = {}): Promise<string> {
  const { signal, onProgress } = hooks;
  if (signal?.aborted) return Promise.reject(abortError());

  if (typeof Worker === 'undefined') return solveOnMainThread(params, hooks);

  return new Promise<string>((resolve, reject) => {
    let worker: Worker;
    try {
      worker = new Worker(new URL('./pow.worker.ts', import.meta.url), { type: 'module' });
    } catch {
      solveOnMainThread(params, hooks).then(resolve, reject);
      return;
    }

    const cleanup = () => {
      worker.terminate();
      signal?.removeEventListener('abort', onAbort);
    };
    const onAbort = () => {
      cleanup();
      reject(abortError());
    };
    signal?.addEventListener('abort', onAbort, { once: true });

    worker.onmessage = (e: MessageEvent<PowMessage>) => {
      const msg = e.data;
      if (msg.type === 'progress') onProgress?.(msg.hashes);
      else if (msg.type === 'done') {
        cleanup();
        resolve(msg.nonce);
      } else {
        cleanup();
        reject(new Error(msg.message));
      }
    };
    // Worker failed to load (CSP, network): use the fallback rather than failing the entry.
    worker.onerror = () => {
      cleanup();
      solveOnMainThread(params, hooks).then(resolve, reject);
    };

    worker.postMessage({ prefix: params.prefix, difficultyBits: params.difficultyBits } satisfies PowRequest);
  });
}

const CHUNK = 20_000;

/** Same search on the main thread, yielding to the event loop between chunks. */
export async function solveOnMainThread(params: SolveParams, hooks: SolveHooks = {}): Promise<string> {
  const { signal, onProgress } = hooks;
  const solver = createChunkedSolver(params.prefix, params.difficultyBits);
  for (;;) {
    if (signal?.aborted) throw abortError();
    const nonce = solver.step(CHUNK);
    if (nonce !== null) return nonce;
    onProgress?.(solver.attempts);
    await new Promise<void>((r) => setTimeout(r, 0));
  }
}
