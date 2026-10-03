import { solvePowSync } from './pow';

/**
 * Proof-of-work runs here so the page stays responsive. The main thread cancels
 * by calling worker.terminate() (a synchronous loop cannot listen for messages).
 */
export interface PowRequest {
  prefix: string;
  difficultyBits: number;
}

export type PowMessage =
  | { type: 'progress'; hashes: number }
  | { type: 'done'; nonce: string; hashes: number }
  | { type: 'error'; message: string };

// The app compiles against the DOM lib, so describe the worker global minimally
// instead of pulling in the conflicting `webworker` lib.
const ctx = self as unknown as {
  onmessage: ((event: MessageEvent<PowRequest>) => void) | null;
  postMessage: (message: PowMessage) => void;
};

ctx.onmessage = (event) => {
  const { prefix, difficultyBits } = event.data;
  try {
    const result = solvePowSync(prefix, difficultyBits, {
      progressEvery: 40_000,
      onProgress: (hashes) => ctx.postMessage({ type: 'progress', hashes }),
    });
    ctx.postMessage({ type: 'done', nonce: result.nonce, hashes: result.hashes });
  } catch (e) {
    ctx.postMessage({ type: 'error', message: e instanceof Error ? e.message : String(e) });
  }
};
