// @vitest-environment jsdom
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ClaimPage } from './ClaimPage';
import { api } from '@/api';
import { ApiError } from '@/api/errors';
import type { EventInfo, StatusResponse } from '@/api/schemas';
import * as solvePow from '@/features/challenge/solvePow';
import { sessionStore } from '@/state/session';
import { serverClock } from '@/lib/serverClock';
import { deferred, renderRoute } from '@/test/render';

// No live pushes needed here: keep the stream open and silent.
vi.mock('@/features/live/streamSse', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/features/live/streamSse')>()),
  streamSse: (o: { signal: AbortSignal }) =>
    new Promise<void>((_r, reject) => o.signal.addEventListener('abort', () => reject(new DOMException('a', 'AbortError')))),
}));

const NOW = '2026-11-01T10:31:00.000Z';
const event: EventInfo = {
  id: 'evt_1',
  name: 'Moonlight Rooftop Sessions',
  phase: 'CLAIMING',
  mode: 'LOTTERY',
  inventory: 500,
  window_opens_at: '2026-11-01T10:00:00.000Z',
  window_closes_at: '2026-11-01T10:30:00.000Z',
  claim_ttl_s: 600,
  server_now: NOW,
};
const won: StatusResponse = { state: 'WON', phase: 'CLAIMING', hold_expires_at: '2026-11-01T10:40:10.000Z', public_id: 'p_1', server_now: NOW };
const claimed = { state: 'CLAIMED' as const, seat_no: 42, ticket_code: 'FD-ABCD-EFGH' };
const KEY = 'fd.claim_key.evt_1';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function setup(status: StatusResponse = won) {
  serverClock.reset();
  serverClock.observe(NOW, Date.now(), Date.now());
  sessionStore.set({ token: 't', user_id: 'u', expires_at: '2099-01-01T00:00:00.000Z' });
  vi.spyOn(api.events, 'get').mockResolvedValue(event);
  const statusSpy = vi.spyOn(api.events, 'status').mockResolvedValue(status);
  const claim = vi.spyOn(api.events, 'claim');
  const utils = renderRoute(<ClaimPage />, { path: '/events/evt_1/claim', route: '/events/:id/claim' });
  return { claim, statusSpy, ...utils };
}

const claimButton = () => screen.findByRole('button', { name: /claim my seat|finish claiming/i });

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  sessionStore.clear();
});

describe('claiming', () => {
  it('claims once with a fresh Idempotency-Key, clears the key, and goes to the ticket', async () => {
    const { claim } = setup();
    claim.mockResolvedValue(claimed);
    expect(await screen.findByTestId('hold-timer')).toBeInTheDocument();
    await userEvent.click(await claimButton());

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/events/evt_1/ticket'));
    expect(claim).toHaveBeenCalledTimes(1);
    const [eventId, key, solution] = claim.mock.calls[0]!;
    expect(eventId).toBe('evt_1');
    expect(key).toMatch(UUID);
    expect(solution).toBeUndefined();
    expect(window.sessionStorage.getItem(KEY)).toBeNull(); // success: the key's job is done
  });

  it('a double tap sends one claim', async () => {
    const d = deferred<typeof claimed>();
    const { claim } = setup();
    claim.mockReturnValue(d.promise);
    const user = userEvent.setup();
    await user.dblClick(await claimButton());
    expect(claim).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: /claiming/i })).toBeDisabled();
    await act(async () => d.resolve(claimed));
  });

  it('after a REFRESH mid-claim, the next tap reuses the SAME Idempotency-Key (no second seat possible)', async () => {
    // 1st page load: tap Claim, the response never comes back, the person refreshes.
    const first = setup();
    first.claim.mockReturnValue(new Promise(() => undefined));
    await userEvent.click(await claimButton());
    await waitFor(() => expect(first.claim).toHaveBeenCalledTimes(1));
    const key1 = first.claim.mock.calls[0]![1];
    expect(window.sessionStorage.getItem(KEY)).toBe(key1);
    first.unmount();
    vi.restoreAllMocks();

    // 2nd page load (same tab, so same sessionStorage): the page knows a claim is pending.
    const second = setup();
    second.claim.mockResolvedValue(claimed);
    expect(await screen.findByText('You started claiming earlier')).toBeInTheDocument();
    await userEvent.click(await screen.findByRole('button', { name: 'Finish claiming' }));
    await waitFor(() => expect(second.claim).toHaveBeenCalledTimes(1));
    expect(second.claim.mock.calls[0]![1]).toBe(key1);
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/ticket'));
  });

  it('retries a dropped connection automatically with the same key, then says honestly that the outcome is unknown', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { claim } = setup();
    claim.mockRejectedValue(new ApiError('NETWORK_ERROR', 'down', 0));
    await userEvent.click(await claimButton());
    for (let i = 0; i < 10 && claim.mock.calls.length < 4; i++) await act(async () => void (await vi.advanceTimersByTimeAsync(10_000)));
    expect(claim).toHaveBeenCalledTimes(4); // first try + 3 retries
    const keys = new Set(claim.mock.calls.map((c) => c[1]));
    expect(keys.size).toBe(1);
    expect(await screen.findByText('We’re not sure your claim went through')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Finish claiming' })).toBeEnabled();
    expect(window.sessionStorage.getItem(KEY)).toBe([...keys][0]); // kept for the next tap
  });

  it('ALREADY_CLAIMED (an earlier attempt won the race) is treated as success: fetch the ticket and show it', async () => {
    const { claim, statusSpy } = setup();
    claim.mockRejectedValue(new ApiError('ALREADY_CLAIMED', 'done', 409));
    statusSpy.mockResolvedValueOnce(won).mockResolvedValue({ ...won, state: 'CLAIMED', seat_no: 42, ticket_code: 'FD-ABCD-EFGH' });
    await userEvent.click(await claimButton());
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/events/evt_1/ticket'));
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
  });

  it('HOLD_EXPIRED: calm explanation, attempt closed (key dropped)', async () => {
    const { claim } = setup();
    claim.mockRejectedValue(new ApiError('HOLD_EXPIRED', 'late', 410));
    await userEvent.click(await claimButton());
    expect(await screen.findByText('Your hold ran out')).toBeInTheDocument();
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
  });

  it('429: no automatic re-fire; countdown, then the SAME key on the next tap', async () => {
    const { claim } = setup();
    claim.mockRejectedValueOnce(new ApiError('RATE_LIMITED', 'slow', 429, undefined, 1200)).mockResolvedValueOnce(claimed);
    const user = userEvent.setup();
    await user.click(await claimButton());
    expect(await screen.findByRole('button', { name: /try again in \ds/i })).toBeDisabled();
    expect(claim).toHaveBeenCalledTimes(1);
    const retry = await screen.findByRole('button', { name: 'Try again' }, { timeout: 3000 });
    await user.click(retry);
    await waitFor(() => expect(claim).toHaveBeenCalledTimes(2));
    expect(claim.mock.calls[1]![1]).toBe(claim.mock.calls[0]![1]);
  });

  it('a challenge on claim is solved and the SAME claim (same key) is repeated with the solution', async () => {
    vi.spyOn(solvePow, 'solvePowInWorker').mockResolvedValue('999');
    const { claim } = setup();
    claim
      .mockRejectedValueOnce(
        new ApiError('CHALLENGE_REQUIRED', 'c', 403, {
          challenge: { id: 'ch_7', type: 'pow', expires_at: '2026-11-01T10:33:00.000Z', pow: { algo: 'sha256-lzb', prefix: 'p', difficulty_bits: 10 } },
        }),
      )
      .mockResolvedValueOnce(claimed);
    await userEvent.click(await claimButton());
    await waitFor(() => expect(claim).toHaveBeenCalledTimes(2));
    expect(claim.mock.calls[1]![1]).toBe(claim.mock.calls[0]![1]);
    expect(claim.mock.calls[1]![2]).toEqual({ id: 'ch_7', solution: '999' });
  });
});

describe('when there is nothing to claim', () => {
  it('already claimed (another tab, or before a refresh): goes straight to the ticket', async () => {
    setup({ ...won, state: 'CLAIMED', seat_no: 1, ticket_code: 'X' });
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/events/evt_1/ticket'));
  });

  it('not a winner: explains and offers the status page, no claim button', async () => {
    setup({ state: 'WAITLISTED', phase: 'CLAIMING', server_now: NOW });
    expect(await screen.findByText('There’s no seat to claim right now')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /claim/i })).not.toBeInTheDocument();
  });
});
