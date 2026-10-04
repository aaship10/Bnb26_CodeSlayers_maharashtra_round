import { ApiError } from '@/api/errors';
import { apiErrorFromResponse } from '@/api/client';
import { createSseParser, type SseMessage } from './sseParser';

export interface StreamSseOptions {
  url: string;
  /** Identity headers (Authorization, X-Device-Id). Accept and Last-Event-ID are added here. */
  headers: Record<string, string>;
  lastEventId: string | null;
  signal: AbortSignal;
  onOpen?: () => void;
  onMessage: (m: SseMessage) => void;
  /** Any bytes at all, heartbeats included. Feeds the stall watchdog. */
  onActivity?: () => void;
  onRetry?: (ms: number) => void;
  fetchImpl?: typeof fetch;
}

/** details.reason on the error when this browser can't stream a response body. */
export const NO_STREAMING = 'no_stream';

/**
 * Open an SSE stream with fetch and feed it through the parser until the server
 * closes it (resolves) or it fails (rejects with an ApiError, or AbortError if
 * the caller aborted). Never retries: reconnecting is the LiveStatus controller's job.
 */
export async function streamSse(o: StreamSseOptions): Promise<void> {
  const doFetch = o.fetchImpl ?? ((...args: Parameters<typeof fetch>) => fetch(...args));
  const headers: Record<string, string> = { ...o.headers, Accept: 'text/event-stream' };
  if (o.lastEventId) headers['Last-Event-ID'] = o.lastEventId;

  let res: Response;
  try {
    res = await doFetch(o.url, { headers, signal: o.signal, cache: 'no-store', credentials: 'same-origin' });
  } catch (cause) {
    if (o.signal.aborted) throw cause;
    throw new ApiError('NETWORK_ERROR', 'Could not open the live stream', 0, undefined, undefined, cause);
  }

  if (!res.ok) throw await apiErrorFromResponse(res);

  const type = res.headers.get('Content-Type') ?? '';
  if (!type.includes('text/event-stream')) {
    void res.body?.cancel();
    throw new ApiError('SCHEMA_MISMATCH', `Expected text/event-stream, got "${type}"`, res.status);
  }
  if (!res.body) throw new ApiError('UNKNOWN', 'This browser cannot read streaming responses', res.status, { reason: NO_STREAMING });

  o.onOpen?.();
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const parser = createSseParser(o.onMessage, o.onRetry);
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      o.onActivity?.();
      parser.push(decoder.decode(value, { stream: true }));
    }
  } catch (cause) {
    if (o.signal.aborted) throw cause;
    throw new ApiError('NETWORK_ERROR', 'The live stream was interrupted', 0, undefined, undefined, cause);
  } finally {
    reader.releaseLock();
  }
}
