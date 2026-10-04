import { ApiError } from '@/api/errors';
import type { StatusResponse } from '@/api/schemas';
import { LiveStatus, isFinalStatus, type ConnectionInfo, type LiveDeps, type StreamArgs } from './LiveStatus';
import { NO_STREAMING } from './streamSse';

const NOW = '2026-11-01T10:10:00.000Z';
const st = (over: Partial<StatusResponse> = {}): StatusResponse => ({ state: 'ENTERED', phase: 'OPEN', server_now: NOW, ...over });

/** A controllable fake SSE connection. */
interface FakeStream {
  args: StreamArgs;
  open(): void;
  send(id: string, status: unknown, event?: string): void;
  heartbeat(): void;
  end(): void;
  fail(e?: unknown): void;
}

function harness(opts: ConstructorParameters<typeof LiveStatus>[2] = {}) {
  const streams: FakeStream[] = [];
  const polls: { resolve: (s: StatusResponse) => void; reject: (e: unknown) => void; signal: AbortSignal }[] = [];
  const statuses: { s: StatusResponse; source: string }[] = [];
  const conns: ConnectionInfo[] = [];

  const deps: LiveDeps = {
    stream: (args) =>
      new Promise<void>((resolve, reject) => {
        args.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
        streams.push({
          args,
          open: () => args.onOpen(),
          send: (id, status, event = 'status') => {
            args.onActivity();
            args.onMessage({ id, event, data: typeof status === 'string' ? status : JSON.stringify(status) });
          },
          heartbeat: () => args.onActivity(),
          end: () => resolve(),
          fail: (e = new ApiError('NETWORK_ERROR', 'drop', 0)) => reject(e),
        });
      }),
    poll: (signal) =>
      new Promise<StatusResponse>((resolve, reject) => {
        signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
        polls.push({ resolve, reject, signal });
      }),
  };

  const live = new LiveStatus(
    deps,
    { onStatus: (s, source) => statuses.push({ s, source }), onConnection: (c) => conns.push(c) },
    { random: () => 0.5, ...opts },
  );
  const last = () => conns[conns.length - 1]!;
  return { live, streams, polls, statuses, conns, last };
}

/** Let promise callbacks (then/catch) run under fake timers. */
const flush = async () => {
  for (let i = 0; i < 5; i++) await Promise.resolve();
};

async function advance(ms: number) {
  await vi.advanceTimersByTimeAsync(ms);
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe('isFinalStatus', () => {
  it('final once claimed, lost, expired, or the event closed', () => {
    expect(isFinalStatus(st({ state: 'CLAIMED', phase: 'CLAIMING' }))).toBe(true);
    expect(isFinalStatus(st({ state: 'LOST', phase: 'CLOSED' }))).toBe(true);
    expect(isFinalStatus(st({ state: 'EXPIRED', phase: 'CLOSED' }))).toBe(true);
    expect(isFinalStatus(st({ state: 'REGISTERED', phase: 'CLOSED' }))).toBe(true);
  });
  it('not final while things can still change (a waitlisted person can still be promoted)', () => {
    expect(isFinalStatus(st({ state: 'ENTERED' }))).toBe(false);
    expect(isFinalStatus(st({ state: 'WON', phase: 'CLAIMING' }))).toBe(false);
    expect(isFinalStatus(st({ state: 'WAITLISTED', phase: 'CLAIMING' }))).toBe(false);
  });
});

describe('SSE path', () => {
  it('connects, goes live, and pushes validated status updates', async () => {
    const h = harness();
    h.live.start();
    expect(h.last().connection).toBe('connecting');
    h.streams[0]!.open();
    expect(h.last().connection).toBe('live');
    h.streams[0]!.send('1', st());
    h.streams[0]!.send('2', st({ phase: 'DRAWING' }));
    expect(h.statuses.map((x) => x.s.phase)).toEqual(['OPEN', 'DRAWING']);
    expect(h.statuses.every((x) => x.source === 'sse')).toBe(true);
    expect(h.live.lastId).toBe('2');
  });

  it('never polls while SSE is healthy', async () => {
    const h = harness();
    h.live.start();
    h.streams[0]!.open();
    for (let i = 0; i < 20; i++) {
      await advance(15_000);
      h.streams[0]!.heartbeat();
    }
    expect(h.polls).toHaveLength(0);
    expect(h.streams).toHaveLength(1);
  });

  it('reconnects after a drop, resuming from Last-Event-ID, with a jittered backoff (not instantly)', async () => {
    const h = harness();
    h.live.start();
    h.streams[0]!.open();
    h.streams[0]!.send('41', st());
    h.streams[0]!.fail();
    await flush();
    expect(h.last()).toMatchObject({ connection: 'reconnecting' });
    const wait = h.last().retryInMs!;
    expect(wait).toBeGreaterThanOrEqual(1000);

    await advance(wait - 1);
    expect(h.streams).toHaveLength(1);
    await advance(1);
    expect(h.streams).toHaveLength(2);
    expect(h.streams[1]!.args.lastEventId).toBe('41');
    h.streams[1]!.open();
    expect(h.last().connection).toBe('live');
  });

  it('a clean server close also reconnects (e.g. backend redeploy)', async () => {
    const h = harness();
    h.live.start();
    h.streams[0]!.open();
    h.streams[0]!.end();
    await flush();
    expect(h.last().connection).toBe('reconnecting');
    await advance(h.last().retryInMs!);
    expect(h.streams).toHaveLength(2);
  });

  it('obeys Retry-After from a 429 on the stream', async () => {
    const h = harness();
    h.live.start();
    h.streams[0]!.fail(new ApiError('RATE_LIMITED', 'slow', 429, undefined, 9_000));
    await flush();
    expect(h.last().retryInMs!).toBeGreaterThanOrEqual(9_000);
    await advance(8_999);
    expect(h.streams).toHaveLength(1);
  });

  it('honours a larger server retry: hint as the backoff base', async () => {
    const h = harness();
    h.live.start();
    h.streams[0]!.args.onRetry(20_000);
    h.streams[0]!.fail();
    await flush();
    expect(h.last().retryInMs!).toBeGreaterThanOrEqual(10_000); // half of a 20 s base, with jitter
  });

  it('a silent stream (no bytes, no heartbeats) is treated as dead and replaced', async () => {
    const h = harness({ heartbeatTimeoutMs: 45_000 });
    h.live.start();
    h.streams[0]!.open();
    await advance(44_000);
    h.streams[0]!.heartbeat(); // a heartbeat resets the watchdog
    await advance(44_000);
    expect(h.streams).toHaveLength(1);
    await advance(2_000);
    expect(h.last()).toMatchObject({ connection: 'reconnecting', error: { code: 'TIMEOUT' } });
    expect(h.streams[0]!.args.signal.aborted).toBe(true);
  });

  it('ignores unknown event types and reports (but survives) an invalid status payload', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const h = harness();
    h.live.start();
    h.streams[0]!.open();
    h.streams[0]!.send('1', { anything: true }, 'announcement');
    h.streams[0]!.send('2', { state: 'MAYBE' });
    h.streams[0]!.send('3', 'not json');
    expect(h.statuses).toHaveLength(0);
    expect(h.conns.some((c) => c.error?.code === 'SCHEMA_MISMATCH')).toBe(true);
    h.streams[0]!.send('4', st());
    expect(h.statuses).toHaveLength(1);
    spy.mockRestore();
  });

  it('never exposes ground truth: a stray sim_label in a pushed status is stripped', async () => {
    const h = harness();
    h.live.start();
    h.streams[0]!.open();
    h.streams[0]!.send('1', { ...st(), sim_label: 'bot' });
    expect(h.statuses[0]!.s).not.toHaveProperty('sim_label');
  });
});

describe('fallback to polling', () => {
  async function failSse(h: ReturnType<typeof harness>, times: number) {
    for (let i = 0; i < times; i++) {
      h.streams[h.streams.length - 1]!.fail();
      await flush();
      if (h.last().connection === 'reconnecting') await advance(h.last().retryInMs!);
    }
  }

  it('switches to polling after 3 SSE failures', async () => {
    const h = harness();
    h.live.start();
    await failSse(h, 3);
    expect(h.last().connection).toBe('polling');
    expect(h.streams).toHaveLength(3);
  });

  it('goes straight to polling when the browser cannot stream, or a proxy mangles the stream', async () => {
    const a = harness();
    a.live.start();
    a.streams[0]!.fail(new ApiError('UNKNOWN', 'no body', 200, { reason: NO_STREAMING }));
    await flush();
    expect(a.last().connection).toBe('polling');

    const b = harness();
    b.live.start();
    b.streams[0]!.fail(new ApiError('SCHEMA_MISMATCH', 'html', 200));
    await flush();
    expect(b.last().connection).toBe('polling');
  });

  it('polls no more often than every 5 s, and spreads clients over 5–10 s', async () => {
    const lo = harness({ random: () => 0 });
    lo.live.start();
    await failSse(lo, 3);
    await advance(lo.last().retryInMs!);
    expect(lo.polls).toHaveLength(1);
    lo.polls[0]!.resolve(st());
    await flush();
    expect(lo.last().retryInMs).toBe(5_000);

    const hi = harness({ random: () => 0.999 });
    hi.live.start();
    await failSse(hi, 3);
    await advance(hi.last().retryInMs!);
    hi.polls[0]!.resolve(st());
    await flush();
    expect(hi.last().retryInMs!).toBeGreaterThan(9_900);
    expect(hi.last().retryInMs!).toBeLessThanOrEqual(10_000);
  });

  it('counts requests over five minutes: at most one per 5 s, i.e. never per-second polling', async () => {
    const h = harness({ random: () => 0, upgradeAfterMs: 10 * 60_000 });
    h.live.start();
    await failSse(h, 3);
    const t0 = Date.now();
    while (Date.now() - t0 < 5 * 60_000) {
      await advance(1_000);
      const p = h.polls[h.polls.length - 1];
      if (p && !p.signal.aborted) {
        p.resolve(st());
        h.polls.pop(); // answered
        h.polls.push({ ...p, resolve: () => undefined, reject: () => undefined, signal: AbortSignal.abort() });
      }
    }
    // 300 s / 5 s = 60 at the very most
    expect(h.statuses.length).toBeLessThanOrEqual(61);
    expect(h.statuses.length).toBeGreaterThan(40);
    expect(h.statuses.every((x) => x.source === 'poll')).toBe(true);
  });

  it('backs off exponentially on poll errors and obeys Retry-After', async () => {
    const h = harness({ random: () => 0, upgradeAfterMs: 60 * 60_000 }); // keep the SSE upgrade attempt out of this test
    h.live.start();
    await failSse(h, 3);
    await advance(h.last().retryInMs!);

    const delays: number[] = [];
    for (let i = 0; i < 4; i++) {
      h.polls[h.polls.length - 1]!.reject(new ApiError('INTERNAL', 'x', 503));
      await flush();
      delays.push(h.last().retryInMs!);
      await advance(h.last().retryInMs!);
    }
    expect(delays[0]).toBeGreaterThanOrEqual(5_000);
    for (let i = 1; i < delays.length; i++) expect(delays[i]!).toBeGreaterThanOrEqual(delays[i - 1]!);
    expect(delays[3]!).toBeGreaterThanOrEqual(20_000);

    h.polls[h.polls.length - 1]!.reject(new ApiError('RATE_LIMITED', 'slow', 429, undefined, 45_000));
    await flush();
    expect(h.last()).toMatchObject({ connection: 'reconnecting', error: { code: 'RATE_LIMITED' } });
    expect(h.last().retryInMs!).toBeGreaterThanOrEqual(45_000);
  });

  it('a successful poll resets the backoff', async () => {
    const h = harness({ random: () => 0 });
    h.live.start();
    await failSse(h, 3);
    await advance(h.last().retryInMs!);
    h.polls[0]!.reject(new ApiError('INTERNAL', 'x', 500));
    await flush();
    await advance(h.last().retryInMs!);
    h.polls[1]!.resolve(st());
    await flush();
    expect(h.last()).toMatchObject({ connection: 'polling', retryInMs: 5_000 });
  });

  it('tries SSE again about once a minute and stays on it if it works', async () => {
    const h = harness({ random: () => 0 });
    h.live.start();
    await failSse(h, 3);
    const before = h.streams.length;
    await advance(60_000);
    expect(h.streams.length).toBe(before + 1);
    h.streams[h.streams.length - 1]!.open();
    expect(h.last().connection).toBe('live');
    const pollsBefore = h.polls.filter((p) => !p.signal.aborted).length;
    await advance(60_000);
    expect(h.polls.filter((p) => !p.signal.aborted).length).toBe(pollsBefore); // no more polling
  });

  it('a failed upgrade drops straight back to polling', async () => {
    const h = harness({ random: () => 0 });
    h.live.start();
    await failSse(h, 3);
    await advance(60_000);
    h.streams[h.streams.length - 1]!.fail();
    await flush();
    expect(h.last().connection).toBe('polling');
  });
});

describe('stopping and pausing', () => {
  it('stops all traffic once the outcome is final', async () => {
    const h = harness();
    h.live.start();
    h.streams[0]!.open();
    h.streams[0]!.send('1', st({ state: 'CLAIMED', phase: 'CLAIMING', seat_no: 7, ticket_code: 'FD-1' }));
    expect(h.last().connection).toBe('final');
    expect(h.streams[0]!.args.signal.aborted).toBe(true);
    await advance(10 * 60_000);
    expect(h.streams).toHaveLength(1);
    expect(h.polls).toHaveLength(0);
  });

  it('stops on errors retrying cannot fix (signed out, forbidden, rejected, not found)', async () => {
    for (const code of ['UNAUTHENTICATED', 'FORBIDDEN', 'REJECTED', 'NOT_FOUND']) {
      const h = harness();
      h.live.start();
      h.streams[0]!.fail(new ApiError(code, 'x', 403));
      await flush();
      expect(h.last()).toMatchObject({ connection: 'stopped', error: { code } });
      await advance(10 * 60_000);
      expect(h.streams).toHaveLength(1);
    }
  });

  it('pausing (hidden tab) drops the connection and every timer; resuming reconnects with a little jitter', async () => {
    const h = harness();
    h.live.start();
    h.streams[0]!.open();
    h.streams[0]!.send('12', st());
    h.live.suspend('paused');
    expect(h.last().connection).toBe('paused');
    expect(h.streams[0]!.args.signal.aborted).toBe(true);
    await advance(30 * 60_000);
    expect(h.streams).toHaveLength(1);
    expect(h.polls).toHaveLength(0);

    h.live.resume();
    expect(h.last().connection).toBe('connecting');
    await advance(1_500);
    expect(h.streams).toHaveLength(2);
    expect(h.streams[1]!.args.lastEventId).toBe('12'); // resumes, so nothing is missed
  });

  it('offline works the same way and pauses polling too', async () => {
    const h = harness({ random: () => 0 });
    h.live.start();
    for (let i = 0; i < 3; i++) {
      h.streams[h.streams.length - 1]!.fail();
      await flush();
      if (h.last().connection === 'reconnecting') await advance(h.last().retryInMs!);
    }
    expect(h.last().connection).toBe('polling');
    h.live.suspend('offline');
    expect(h.last().connection).toBe('offline');
    await advance(10 * 60_000);
    expect(h.polls).toHaveLength(0);
    h.live.resume();
    await advance(1_500);
    expect(h.streams.length).toBe(4); // back online: try SSE first
  });

  it('stop() is final: nothing restarts afterwards', async () => {
    const h = harness();
    h.live.start();
    h.streams[0]!.fail();
    await flush();
    h.live.stop();
    await advance(10 * 60_000);
    expect(h.streams).toHaveLength(1);
    h.live.resume();
    await advance(10_000);
    expect(h.streams).toHaveLength(1);
  });
});
