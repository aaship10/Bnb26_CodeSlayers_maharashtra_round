import { bestCrypto } from './cryptoImpl';
import { entrantsSchema, resultsSchema, type Fairness, type ResultsResponse } from './schemas';
import { verifyDraw, type Step, type VerifyOutcome } from './verifyDraw';

/**
 * Runs the whole verification off the main thread: downloads the entrant list
 * (about 2 MB for 50,000 entries), validates it, and recomputes the draw. The
 * page only receives step updates, progress, and finally the draw order.
 * Cancel by terminating the worker.
 */
export interface VerifyRequest {
  eventId: string;
  apiBase: string;
  deviceId: string;
  fairness: Fairness;
}

export type VerifyMessage =
  | { type: 'steps'; steps: Step[] }
  | { type: 'progress'; fraction: number }
  | { type: 'done'; outcome: VerifyOutcome; engine: string }
  | { type: 'error'; message: string };

const ctx = self as unknown as {
  onmessage: ((e: MessageEvent<VerifyRequest>) => void) | null;
  postMessage: (m: VerifyMessage) => void;
};

async function getJson(url: string, deviceId: string): Promise<unknown> {
  const res = await fetch(url, { headers: { Accept: 'application/json', 'X-Device-Id': deviceId }, cache: 'no-store' });
  if (!res.ok) throw new Error(`HTTP ${res.status} from ${new URL(url, self.location.href).pathname}`);
  return res.json();
}

ctx.onmessage = async (e) => {
  const { eventId, apiBase, deviceId, fairness } = e.data;
  const base = `${apiBase}/events/${encodeURIComponent(eventId)}/fairness`;
  let results: Promise<ResultsResponse | null> | null = null;
  const crypto = bestCrypto();
  let lastProgress = 0;

  try {
    const outcome = await verifyDraw(
      {
        eventId,
        fairness,
        loadEntrants: async () => {
          const parsed = entrantsSchema.safeParse(await getJson(`${base}/entrants`, deviceId));
          if (!parsed.success) throw new Error(`the entrant list isn’t in the expected format (${parsed.error.issues[0]?.message})`);
          return parsed.data.entrants;
        },
        // Optional: compared entry by entry if the API publishes the ordered lists.
        loadResults: () =>
          (results ??= getJson(`${base}/results`, deviceId)
            .then((j) => {
              const p = resultsSchema.safeParse(j);
              return p.success ? p.data : null;
            })
            .catch(() => null)),
      },
      crypto,
      {
        onSteps: (steps) => ctx.postMessage({ type: 'steps', steps }),
        onProgress: (fraction) => {
          if (fraction - lastProgress >= 0.02 || fraction === 1) {
            lastProgress = fraction;
            ctx.postMessage({ type: 'progress', fraction });
          }
        },
      },
    );
    ctx.postMessage({ type: 'done', outcome, engine: crypto.name });
  } catch (err) {
    ctx.postMessage({ type: 'error', message: err instanceof Error ? err.message : String(err) });
  }
};
