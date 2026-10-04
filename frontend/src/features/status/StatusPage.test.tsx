// @vitest-environment jsdom
import { act, screen, waitFor } from '@testing-library/react';
import { StatusPage } from './StatusPage';
import { api } from '@/api';
import { ApiError } from '@/api/errors';
import type { EventInfo, StatusResponse } from '@/api/schemas';
import { sessionStore } from '@/state/session';
import { serverClock } from '@/lib/serverClock';
import { renderRoute } from '@/test/render';
import type { StreamSseOptions } from '@/features/live/streamSse';

/** A controllable fake for the SSE reader: tests push events and break connections. */
const fake = vi.hoisted(() => {
  const streams: { o: StreamSseOptions; fail: (e: unknown) => void }[] = [];
  return { streams };
});

vi.mock('@/features/live/streamSse', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/features/live/streamSse')>();
  return {
    ...orig,
    streamSse: (o: StreamSseOptions) =>
      new Promise<void>((_res, reject) => {
        o.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
        fake.streams.push({ o, fail: reject });
      }),
  };
});

const NOW = '2026-11-01T10:31:00.000Z';
const event: EventInfo = {
  id: 'evt_1',
  name: 'Moonlight Rooftop Sessions',
  phase: 'DRAWING',
  mode: 'LOTTERY',
  inventory: 500,
  window_opens_at: '2026-11-01T10:00:00.000Z',
  window_closes_at: '2026-11-01T10:30:00.000Z',
  claim_ttl_s: 600,
  server_now: NOW,
};
const st = (over: Partial<StatusResponse> = {}): StatusResponse => ({ state: 'ENTERED', phase: 'OPEN', server_now: NOW, ...over });

let seq = 0;
function push(status: StatusResponse) {
  const s = fake.streams[fake.streams.length - 1]!;
  act(() => {
    s.o.onActivity?.();
    s.o.onMessage({ id: String(++seq), event: 'status', data: JSON.stringify(status) });
  });
}
function open() {
  act(() => fake.streams[fake.streams.length - 1]!.o.onOpen?.());
}

function setup(initial: StatusResponse | ApiError, { signedIn = true } = {}) {
  serverClock.reset();
  serverClock.observe(NOW, Date.now(), Date.now());
  if (signedIn) sessionStore.set({ token: 't', user_id: 'u', expires_at: '2099-01-01T00:00:00.000Z' });
  vi.spyOn(api.events, 'get').mockResolvedValue(event);
  const status = vi.spyOn(api.events, 'status').mockImplementation(() => (initial instanceof ApiError ? Promise.reject(initial) : Promise.resolve(initial)));
  const utils = renderRoute(<StatusPage />, { path: '/events/evt_1/status', route: '/events/:id/status' });
  return { status, ...utils };
}

beforeEach(() => {
  fake.streams.length = 0;
  seq = 0;
  window.localStorage.clear();
});
afterEach(() => {
  vi.restoreAllMocks();
  sessionStore.clear();
});

describe('StatusPage, live', () => {
  it('follows the whole story by itself: waiting -> drawing -> won -> claimed, then stops listening', async () => {
    const { status } = setup(st());
    expect(await screen.findByRole('heading', { name: 'You’re in the draw' })).toBeInTheDocument();
    await waitFor(() => expect(fake.streams).toHaveLength(1));
    open();
    expect(screen.getByTestId('live-indicator')).toHaveAttribute('data-connection', 'live');

    push(st({ phase: 'DRAWING' }));
    expect(screen.getByRole('heading', { name: 'The draw is running' })).toBeInTheDocument();
    expect(screen.getByTestId('announcer')).toHaveTextContent('The draw is running.');

    push(st({ state: 'WON', phase: 'CLAIMING', hold_expires_at: '2026-11-01T10:40:10.000Z', public_id: 'p_1' }));
    expect(screen.getByRole('heading', { name: 'You’ve been picked!' })).toBeInTheDocument();
    expect(screen.getByTestId('hold-timer')).toBeInTheDocument();

    push(st({ state: 'CLAIMED', phase: 'CLAIMING', seat_no: 7, ticket_code: 'FD-1' }));
    expect(screen.getByRole('heading', { name: 'Your seat is confirmed' })).toBeInTheDocument();
    expect(screen.getByTestId('live-indicator')).toHaveAttribute('data-connection', 'final');
    expect(fake.streams[0]!.o.signal.aborted).toBe(true); // final: connection released

    expect(status).toHaveBeenCalledTimes(1); // one read at load; everything else arrived pushed
  });

  it('sends identity headers on the stream, never a token in the URL', async () => {
    setup(st());
    await waitFor(() => expect(fake.streams).toHaveLength(1));
    const o = fake.streams[0]!.o;
    expect(o.url).toBe('/api/events/evt_1/stream');
    expect(o.headers['authorization']).toBe('Bearer t');
    expect(o.headers['x-device-id']).toBeTruthy();
    expect(o.url).not.toMatch(/token|Bearer|t=/);
  });

  it('keeps showing the last known state, clearly marked, while reconnecting', async () => {
    setup(st());
    await waitFor(() => expect(fake.streams).toHaveLength(1));
    open();
    push(st({ phase: 'DRAWING' }));
    await act(async () => fake.streams[0]!.fail(new ApiError('NETWORK_ERROR', 'drop', 0)));
    expect(await screen.findByText('Reconnecting…', { selector: 'p' })).toBeInTheDocument();
    expect(screen.getByText(/Showing what we knew at/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'The draw is running' })).toBeInTheDocument();
    expect(screen.getByTestId('live-indicator')).toHaveAttribute('data-connection', 'reconnecting');
  });

  it('pauses while the tab is hidden and resumes when it is visible again', async () => {
    setup(st());
    await waitFor(() => expect(fake.streams).toHaveLength(1));
    open();
    const vis = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    act(() => void document.dispatchEvent(new Event('visibilitychange')));
    expect(screen.getByTestId('live-indicator')).toHaveAttribute('data-connection', 'paused');
    expect(fake.streams[0]!.o.signal.aborted).toBe(true);

    vis.mockReturnValue('visible');
    act(() => void document.dispatchEvent(new Event('visibilitychange')));
    await waitFor(() => expect(fake.streams).toHaveLength(2), { timeout: 3000 });
    expect(fake.streams[1]!.o.lastEventId).toBeNull(); // nothing had been received yet on the first stream
  });

  it('does not open a stream at all when the result is already final', async () => {
    setup(st({ state: 'LOST', phase: 'CLOSED' }));
    expect(await screen.findByRole('heading', { name: 'Not this time' })).toBeInTheDocument();
    expect(screen.getByTestId('live-indicator')).toHaveAttribute('data-connection', 'final');
    await new Promise((r) => setTimeout(r, 50));
    expect(fake.streams).toHaveLength(0);
  });

  it('explains a REJECTED status read kindly', async () => {
    setup(new ApiError('REJECTED', 'no', 403));
    await waitFor(() => expect(fake.streams).toHaveLength(1));
    await act(async () => fake.streams[0]!.fail(new ApiError('REJECTED', 'no', 403)));
    expect(await screen.findByText("We couldn't accept this request")).toBeInTheDocument();
  });

  it('a rate-limited first read shows a countdown and recovers through the live stream', async () => {
    setup(new ApiError('RATE_LIMITED', 'slow', 429, { retry_after_ms: 5000 }, 5000));
    await waitFor(() => expect(fake.streams).toHaveLength(1));
    await act(async () => fake.streams[0]!.fail(new ApiError('RATE_LIMITED', 'slow', 429, undefined, 5000)));
    expect(await screen.findByText('Let’s slow down a little')).toBeInTheDocument();
    expect(screen.getByText(/check again in \d+s/)).toBeInTheDocument();
  });

  it('signed out: asks to sign in and opens nothing', async () => {
    setup(st(), { signedIn: false });
    expect(screen.getByRole('heading', { name: 'Sign in to see your status' })).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 30));
    expect(fake.streams).toHaveLength(0);
  });
});
