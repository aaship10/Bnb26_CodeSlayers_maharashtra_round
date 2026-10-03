import { createSseParser, type SseMessage } from './sseParser';
import { NO_STREAMING, streamSse } from './streamSse';

function parseAll(chunks: string[]) {
  const out: SseMessage[] = [];
  const retries: number[] = [];
  const p = createSseParser((m) => out.push(m), (ms) => retries.push(ms));
  for (const c of chunks) p.push(c);
  return { out, retries };
}

describe('SSE parser (WHATWG rules)', () => {
  it('parses a typical status event', () => {
    const { out } = parseAll(['id: 7\nevent: status\ndata: {"a":1}\n\n']);
    expect(out).toEqual([{ id: '7', event: 'status', data: '{"a":1}' }]);
  });

  it('defaults the event name to "message" and joins multi-line data with \\n', () => {
    const { out } = parseAll(['data: one\ndata: two\n\n']);
    expect(out).toEqual([{ id: undefined, event: 'message', data: 'one\ntwo' }]);
  });

  it('handles events split at every possible byte boundary', () => {
    const raw = 'id: 1\nevent: status\ndata: {"x":"y"}\n\nid: 2\ndata: second\n\n';
    for (let cut = 1; cut < raw.length; cut++) {
      const { out } = parseAll([raw.slice(0, cut), raw.slice(cut)]);
      expect(out, `cut at ${cut}`).toEqual([
        { id: '1', event: 'status', data: '{"x":"y"}' },
        { id: '2', event: 'message', data: 'second' },
      ]);
    }
  });

  it('accepts CRLF and lone CR line endings, including a CRLF split across chunks', () => {
    expect(parseAll(['data: a\r\n\r\n']).out).toHaveLength(1);
    expect(parseAll(['data: a\r\r']).out).toEqual([{ id: undefined, event: 'message', data: 'a' }]);
    // CR ends chunk one, LF starts chunk two: must count as ONE line break, not two
    const split = parseAll(['data: a\r', '\ndata: b\r\n\r\n']);
    expect(split.out).toEqual([{ id: undefined, event: 'message', data: 'a\nb' }]);
  });

  it('ignores comments (heartbeats) and unknown fields', () => {
    const { out } = parseAll([': ping\n\n', 'foo: bar\ndata: x\n\n', ':another\n']);
    expect(out).toEqual([{ id: undefined, event: 'message', data: 'x' }]);
  });

  it('drops events without data but keeps their id for later events', () => {
    const { out } = parseAll(['id: 5\n\n', 'data: after\n\n']);
    expect(out).toEqual([{ id: '5', event: 'message', data: 'after' }]);
  });

  it('id persists across events until changed; ids containing NUL are ignored', () => {
    const { out } = parseAll(['id: 3\ndata: a\n\n', 'data: b\n\n', 'id: 4\u00005\ndata: c\n\n']);
    expect(out.map((m) => m.id)).toEqual(['3', '3', '3']);
  });

  it('removes exactly one leading space after the colon; a field with no colon has an empty value', () => {
    const { out } = parseAll(['data:  two spaces\n\n', 'data\n\n']);
    expect(out.map((m) => m.data)).toEqual([' two spaces', '']);
  });

  it('reports retry hints and ignores non-numeric ones', () => {
    const { retries } = parseAll(['retry: 3000\n', 'retry: soon\n', 'retry: 15\n']);
    expect(retries).toEqual([3000, 15]);
  });

  it('strips a leading BOM', () => {
    expect(parseAll(['﻿data: x\n\n']).out).toHaveLength(1);
  });

  it('an unterminated event at the end of the stream is not dispatched', () => {
    expect(parseAll(['data: half']).out).toEqual([]);
  });
});

/* ---------------------------------------------------------------- streamSse */

function sseResponse(chunks: string[], init: ResponseInit = {}, opts: { failAfter?: boolean } = {}) {
  const enc = new TextEncoder();
  let i = 0;
  // pull-based so a failure arrives AFTER the earlier chunks were read, like a real connection reset
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) controller.enqueue(enc.encode(chunks[i++]!));
      else if (opts.failAfter) controller.error(new TypeError('network reset'));
      else controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' }, ...init });
}

describe('streamSse', () => {
  it('sends identity headers, Accept and Last-Event-ID, then delivers messages until EOF', async () => {
    let seen: Headers | undefined;
    const fetchImpl = (async (_url: string, init: RequestInit) => {
      seen = new Headers(init.headers);
      return sseResponse(['retry: 3000\n\n', 'id: 9\nevent: status\ndata: {}\n\n', ': ping\n\n']);
    }) as unknown as typeof fetch;
    const msgs: SseMessage[] = [];
    const onOpen = vi.fn();
    const onActivity = vi.fn();
    const onRetry = vi.fn();
    await streamSse({
      url: '/api/events/e/stream',
      headers: { Authorization: 'Bearer t', 'X-Device-Id': 'd' },
      lastEventId: '8',
      signal: new AbortController().signal,
      onOpen,
      onMessage: (m) => msgs.push(m),
      onActivity,
      onRetry,
      fetchImpl,
    });
    expect(seen?.get('Authorization')).toBe('Bearer t');
    expect(seen?.get('X-Device-Id')).toBe('d');
    expect(seen?.get('Accept')).toBe('text/event-stream');
    expect(seen?.get('Last-Event-ID')).toBe('8');
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(msgs).toEqual([{ id: '9', event: 'status', data: '{}' }]);
    expect(onActivity).toHaveBeenCalledTimes(3);
    expect(onRetry).toHaveBeenCalledWith(3000);
  });

  it('omits Last-Event-ID on a first connection', async () => {
    let seen: Headers | undefined;
    const fetchImpl = (async (_u: string, init: RequestInit) => {
      seen = new Headers(init.headers);
      return sseResponse([]);
    }) as unknown as typeof fetch;
    await streamSse({ url: '/s', headers: {}, lastEventId: null, signal: new AbortController().signal, onMessage: () => undefined, fetchImpl });
    expect(seen?.has('Last-Event-ID')).toBe(false);
  });

  it('turns an error response into the matching ApiError (e.g. 429 with Retry-After)', async () => {
    const fetchImpl = (async () =>
      new Response(JSON.stringify({ code: 'RATE_LIMITED', message: 'slow', details: { retry_after_ms: 4000 } }), {
        status: 429,
        headers: { 'Content-Type': 'application/json', 'Retry-After': '4' },
      })) as unknown as typeof fetch;
    await expect(
      streamSse({ url: '/s', headers: {}, lastEventId: null, signal: new AbortController().signal, onMessage: () => undefined, fetchImpl }),
    ).rejects.toMatchObject({ code: 'RATE_LIMITED', retryAfterMs: 4000 });
  });

  it('rejects a non-SSE content type (e.g. a proxy returning HTML)', async () => {
    const fetchImpl = (async () => new Response('<html/>', { status: 200, headers: { 'Content-Type': 'text/html' } })) as unknown as typeof fetch;
    await expect(
      streamSse({ url: '/s', headers: {}, lastEventId: null, signal: new AbortController().signal, onMessage: () => undefined, fetchImpl }),
    ).rejects.toMatchObject({ code: 'SCHEMA_MISMATCH' });
  });

  it('reports a mid-stream drop as NETWORK_ERROR after delivering what arrived', async () => {
    const msgs: SseMessage[] = [];
    const fetchImpl = (async () => sseResponse(['id: 1\ndata: a\n\n'], {}, { failAfter: true })) as unknown as typeof fetch;
    await expect(
      streamSse({ url: '/s', headers: {}, lastEventId: null, signal: new AbortController().signal, onMessage: (m) => msgs.push(m), fetchImpl }),
    ).rejects.toMatchObject({ code: 'NETWORK_ERROR' });
    expect(msgs).toHaveLength(1);
  });

  it('flags browsers that cannot stream bodies so the controller goes straight to polling', async () => {
    const res = new Response(null, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    const fetchImpl = (async () => res) as unknown as typeof fetch;
    await expect(
      streamSse({ url: '/s', headers: {}, lastEventId: null, signal: new AbortController().signal, onMessage: () => undefined, fetchImpl }),
    ).rejects.toMatchObject({ details: { reason: NO_STREAMING } });
  });

  it('a refused connection is NETWORK_ERROR; a caller abort is passed through untouched', async () => {
    const refused = (async () => {
      throw new TypeError('Failed to fetch');
    }) as unknown as typeof fetch;
    await expect(
      streamSse({ url: '/s', headers: {}, lastEventId: null, signal: new AbortController().signal, onMessage: () => undefined, fetchImpl: refused }),
    ).rejects.toMatchObject({ code: 'NETWORK_ERROR' });

    const ctl = new AbortController();
    ctl.abort();
    const aborting = (async () => {
      throw new DOMException('aborted', 'AbortError');
    }) as unknown as typeof fetch;
    await expect(
      streamSse({ url: '/s', headers: {}, lastEventId: null, signal: ctl.signal, onMessage: () => undefined, fetchImpl: aborting }),
    ).rejects.toMatchObject({ name: 'AbortError' });
  });
});
