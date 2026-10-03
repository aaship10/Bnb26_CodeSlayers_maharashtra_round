// @vitest-environment jsdom
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { EventPage } from './EventPage';
import { api } from '@/api';
import { ApiError } from '@/api/errors';
import type { EventInfo, StatusResponse } from '@/api/schemas';
import * as solvePow from '@/features/challenge/solvePow';
import { sessionStore } from '@/state/session';
import { serverClock } from '@/lib/serverClock';
import { deferred, renderRoute } from '@/test/render';

const NOW = '2026-11-01T10:10:00.000Z';

const event = (over: Partial<EventInfo> = {}): EventInfo => ({
  id: 'evt_1',
  name: 'Moonlight Rooftop Sessions',
  description: 'One night, 500 seats.',
  phase: 'OPEN',
  mode: 'LOTTERY',
  inventory: 500,
  window_opens_at: '2026-11-01T10:00:00.000Z',
  window_closes_at: '2026-11-01T10:30:00.000Z',
  claim_ttl_s: 600,
  seed_commitment: 'a'.repeat(64),
  server_now: NOW,
  ...over,
});

const status = (over: Partial<StatusResponse> = {}): StatusResponse => ({ state: 'REGISTERED', phase: 'OPEN', server_now: NOW, ...over });

const enterOk = { state: 'ENTERED' as const, entered_at: NOW, already_entered: false };

const powChallenge = {
  id: 'ch_1',
  type: 'pow',
  expires_at: '2026-11-01T10:12:00.000Z',
  pow: { algo: 'sha256-lzb', prefix: 'fd1.evt.1', difficulty_bits: 14 },
};
const captchaChallenge = { id: 'ch_2', type: 'captcha', expires_at: '2026-11-01T10:12:00.000Z', captcha: { provider: 'mock', site_key: 'k' } };
const challengeErr = (c: unknown) => new ApiError('CHALLENGE_REQUIRED', 'Challenge required', 403, { challenge: c });

function setup({ ev = event(), st = status(), signedIn = true }: { ev?: EventInfo; st?: StatusResponse | ApiError; signedIn?: boolean } = {}) {
  serverClock.reset();
  serverClock.observe(NOW, Date.now(), Date.now());
  if (signedIn) sessionStore.set({ token: 't', user_id: 'u1', expires_at: '2099-01-01T00:00:00.000Z' });
  vi.spyOn(api.events, 'get').mockResolvedValue(ev);
  const statusSpy = vi
    .spyOn(api.events, 'status')
    .mockImplementation(() => (st instanceof ApiError ? Promise.reject(st) : Promise.resolve(st)));
  const enter = vi.spyOn(api.events, 'enter');
  const utils = renderRoute(<EventPage />, { path: '/events/evt_1', route: '/events/:id' });
  return { enter, statusSpy, ...utils };
}

beforeEach(() => {
  window.localStorage.clear();
  sessionStore.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  sessionStore.clear();
});

describe('event details', () => {
  it('shows the essentials and the no-rush promise for a lottery', async () => {
    setup();
    expect(await screen.findByRole('heading', { level: 1, name: 'Moonlight Rooftop Sessions' })).toBeInTheDocument();
    expect(screen.getByText('Window open')).toBeInTheDocument();
    expect(screen.getByText('No need to rush')).toBeInTheDocument();
    expect(screen.getByText('500')).toBeInTheDocument();
    expect(screen.getByText('10 minutes to claim')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /how we prove it/i })).toHaveAttribute('href', '/events/evt_1/fairness');
    expect(screen.getByText('aaaaaaaa…aaaaaa')).toBeInTheDocument(); // shortened commitment
  });

  it('does NOT claim "no need to rush" for first-come-first-served', async () => {
    setup({ ev: event({ mode: 'FCFS' }) });
    expect(await screen.findByText('First come, first served')).toBeInTheDocument();
    expect(screen.queryByText('No need to rush')).not.toBeInTheDocument();
  });

  it('sets the page title', async () => {
    setup();
    await screen.findByRole('heading', { level: 1 });
    expect(document.title).toBe('Moonlight Rooftop Sessions · Fair Drop');
  });

  it('a missing event gets a friendly page, not a crash', async () => {
    serverClock.reset();
    vi.spyOn(api.events, 'get').mockRejectedValue(new ApiError('NOT_FOUND', 'nope', 404));
    renderRoute(<EventPage />, { path: '/events/nope', route: '/events/:id' });
    expect(await screen.findByText("We couldn't find that")).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /back to the drops/i })).toHaveAttribute('href', '/');
  });

  it('a server error offers a retry', async () => {
    serverClock.reset();
    const get = vi.spyOn(api.events, 'get').mockRejectedValueOnce(new ApiError('INTERNAL', 'x', 500)).mockResolvedValue(event());
    renderRoute(<EventPage />, { path: '/events/evt_1', route: '/events/:id' });
    expect(await screen.findByText('Something went wrong on our side')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /try again/i }));
    expect(await screen.findByRole('heading', { level: 1, name: 'Moonlight Rooftop Sessions' })).toBeInTheDocument();
    expect(get).toHaveBeenCalledTimes(2);
  });
});

describe('before the window', () => {
  it('shows a countdown to the opening, and the Enter control is off', async () => {
    const future = { phase: 'SCHEDULED' as const, window_opens_at: '2026-11-01T10:20:00.000Z', window_closes_at: '2026-11-01T10:50:00.000Z' };
    setup({ ev: event(future), st: status({ phase: 'SCHEDULED' }) });
    expect(await screen.findByText('Entries open in')).toBeInTheDocument();
    expect(screen.getByTestId('countdown-tiles').textContent).toMatch(/^00hrs(09|10)min\d\dsec$/);
    expect(screen.getByRole('button', { name: 'Entries open soon' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: /enter the draw/i })).not.toBeInTheDocument();
  });

  it('when the countdown reaches zero but the SERVER still says SCHEDULED, nothing unlocks: "any moment now"', async () => {
    // opens_at (10:00) is already behind server-now (10:10) yet the server reports SCHEDULED
    setup({ ev: event({ phase: 'SCHEDULED' }), st: status({ phase: 'SCHEDULED' }) });
    expect(await screen.findByText('Any moment now…')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Entries open soon' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: /enter the draw/i })).not.toBeInTheDocument();
  });

  it('invites signed-out visitors to sign in ahead of time, remembering where they were', async () => {
    setup({ ev: event({ phase: 'SCHEDULED' }), signedIn: false });
    const link = await screen.findByRole('link', { name: /sign in ahead of time/i });
    expect(link).toHaveAttribute('href', '/register');
  });

  it('a draft event is not announced', async () => {
    setup({ ev: event({ phase: 'DRAFT' }) });
    expect(await screen.findByText('Not announced yet')).toBeInTheDocument();
  });
});

describe('signed out, window open', () => {
  it('asks to sign in and does not touch the authenticated endpoints', async () => {
    const { enter, statusSpy } = setup({ signedIn: false });
    expect(await screen.findByRole('link', { name: /sign in to enter/i })).toBeInTheDocument();
    expect(statusSpy).not.toHaveBeenCalled();
    expect(enter).not.toHaveBeenCalled();
  });
});

describe('entering', () => {
  it('one tap enters; a second tap while pending does nothing; success is explained', async () => {
    const d = deferred<typeof enterOk>();
    const { enter } = setup();
    enter.mockReturnValue(d.promise);
    const user = userEvent.setup();

    const button = await screen.findByRole('button', { name: 'Enter the draw' });
    await user.click(button);
    expect(screen.getByRole('button', { name: /entering/i })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: /entering/i }));
    expect(enter).toHaveBeenCalledTimes(1);
    expect(enter.mock.calls[0]![0]).toBe('evt_1');
    expect(enter.mock.calls[0]![1]).toBeUndefined(); // no challenge solution on the first try

    await act(async () => d.resolve(enterOk));
    expect(await screen.findByText('You’re in the draw')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /see my status/i })).toHaveAttribute('href', '/events/evt_1/status');
    expect(screen.queryByRole('button', { name: /enter the draw/i })).not.toBeInTheDocument();
    expect(screen.getByTestId('announcer')).toHaveTextContent('You are entered in the draw.');
  });

  it('says so, calmly, when the person had already entered', async () => {
    const { enter } = setup();
    enter.mockResolvedValue({ ...enterOk, already_entered: true });
    await userEvent.click(await screen.findByRole('button', { name: 'Enter the draw' }));
    expect(await screen.findByText(/you had already entered, so nothing changed/i)).toBeInTheDocument();
  });

  it('after a refresh, someone who already entered sees that straight away (state comes from the server)', async () => {
    const { enter } = setup({ st: status({ state: 'ENTERED' }) });
    expect(await screen.findByText('You’re in the draw')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /enter the draw/i })).not.toBeInTheDocument();
    expect(enter).not.toHaveBeenCalled();
  });

  it('still offers Enter if the status lookup fails (entering is idempotent)', async () => {
    setup({ st: new ApiError('INTERNAL', 'x', 500) });
    expect(await screen.findByRole('button', { name: 'Enter the draw' })).toBeEnabled();
  });

  it('after the window: entrants are sent to their status, others are told entries are closed', async () => {
    const a = setup({ ev: event({ phase: 'CLOSED' }), st: status({ phase: 'CLOSED', state: 'ENTERED' }) });
    expect(await screen.findByRole('link', { name: /see my status/i })).toBeInTheDocument();
    a.unmount();

    setup({ ev: event({ phase: 'CLOSED' }), st: status({ phase: 'CLOSED' }) });
    expect(await screen.findByText('Entries are closed')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /enter/i })).not.toBeInTheDocument();
  });
});

describe('failure handling', () => {
  it('429: explains, counts down, keeps the button off until Retry-After, then lets the person tap again', async () => {
    const { enter } = setup();
    enter.mockRejectedValueOnce(new ApiError('RATE_LIMITED', 'slow', 429, { retry_after_ms: 1300, scope: 'user' }, 1300)).mockResolvedValueOnce(enterOk);
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: 'Enter the draw' }));
    expect(await screen.findByText('Let’s slow down a little')).toBeInTheDocument();
    const waiting = screen.getByRole('button', { name: /try again in \ds/i });
    expect(waiting).toBeDisabled();
    expect(enter).toHaveBeenCalledTimes(1); // no automatic re-fire

    const retry = await screen.findByRole('button', { name: 'Try again' }, { timeout: 3000 });
    expect(retry).toBeEnabled();
    await user.click(retry);
    expect(await screen.findByText('You’re in the draw')).toBeInTheDocument();
    expect(enter).toHaveBeenCalledTimes(2);
  });

  it('REJECTED is shown kindly and the button stays available', async () => {
    const { enter } = setup();
    enter.mockRejectedValue(new ApiError('REJECTED', 'no', 403));
    await userEvent.click(await screen.findByRole('button', { name: 'Enter the draw' }));
    expect(await screen.findByText("We couldn't accept this request")).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Enter the draw' })).toBeEnabled();
  });

  it('WINDOW_CLOSED refreshes the event so the page catches up with the server', async () => {
    const { enter } = setup();
    enter.mockRejectedValue(new ApiError('WINDOW_CLOSED', 'closed', 409));
    const get = vi.mocked(api.events.get);
    await userEvent.click(await screen.findByRole('button', { name: 'Enter the draw' }));
    expect(await screen.findByText('The entry window has closed')).toBeInTheDocument();
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it('an expired session turns the card back into "Sign in"', async () => {
    const { enter } = setup();
    enter.mockImplementation(async () => {
      sessionStore.clear(); // what the global UNAUTHENTICATED handler does
      throw new ApiError('UNAUTHENTICATED', 'expired', 401);
    });
    await userEvent.click(await screen.findByRole('button', { name: 'Enter the draw' }));
    expect(await screen.findByRole('link', { name: /sign in to enter/i })).toBeInTheDocument();
  });
});

describe('challenges', () => {
  it('proof-of-work: shows "Verifying you’re human…", solves off-thread, repeats the same request with the solution', async () => {
    const solving = deferred<string>();
    const solve = vi.spyOn(solvePow, 'solvePowInWorker').mockReturnValue(solving.promise);
    const { enter } = setup();
    enter.mockRejectedValueOnce(challengeErr(powChallenge)).mockResolvedValueOnce(enterOk);

    await userEvent.click(await screen.findByRole('button', { name: 'Enter the draw' }));

    expect(await screen.findByText('Verifying you’re human…')).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toBeInTheDocument();
    expect(solve).toHaveBeenCalledWith({ prefix: 'fd1.evt.1', difficultyBits: 14 }, expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(screen.queryByRole('button', { name: 'Enter the draw' })).not.toBeInTheDocument(); // can't double-fire meanwhile

    await act(async () => solving.resolve('31337'));
    expect(await screen.findByText('You’re in the draw')).toBeInTheDocument();

    expect(enter).toHaveBeenCalledTimes(2);
    expect(enter.mock.calls[0]![1]).toBeUndefined();
    expect(enter.mock.calls[1]![1]).toEqual({ id: 'ch_1', solution: '31337' }); // X-Challenge-Id / -Solution
    expect(screen.queryByText('Verifying you’re human…')).not.toBeInTheDocument();
  });

  it('CAPTCHA: renders the widget with an accessible alternative, then retries with the token', async () => {
    const { enter } = setup();
    enter.mockRejectedValueOnce(challengeErr(captchaChallenge)).mockResolvedValueOnce(enterOk);
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: 'Enter the draw' }));
    const box = await screen.findByRole('checkbox', { name: /i’m a person/i });
    expect(screen.getByText(/can’t use this check/i)).toBeInTheDocument(); // the accessible alternative
    await user.click(box);

    expect(await screen.findByText('You’re in the draw', {}, { timeout: 3000 })).toBeInTheDocument();
    expect(enter.mock.calls[1]![1]).toEqual({ id: 'ch_2', solution: 'mock-captcha-ok' });
  });

  it('cancelling a challenge returns to a ready state without entering', async () => {
    vi.spyOn(solvePow, 'solvePowInWorker').mockImplementation(
      (_p, hooks) =>
        new Promise((_res, rej) => hooks?.signal?.addEventListener('abort', () => rej(new DOMException('cancelled', 'AbortError')))),
    );
    const { enter } = setup();
    enter.mockRejectedValue(challengeErr(powChallenge));
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: 'Enter the draw' }));
    await screen.findByText('Verifying you’re human…');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(await screen.findByRole('button', { name: 'Enter the draw' })).toBeEnabled();
    expect(screen.queryByText('Verifying you’re human…')).not.toBeInTheDocument();
    expect(enter).toHaveBeenCalledTimes(1);
  });

  it('leaving the page terminates the solver', async () => {
    let aborted = false;
    vi.spyOn(solvePow, 'solvePowInWorker').mockImplementation(
      (_p, hooks) =>
        new Promise((_res, rej) =>
          hooks?.signal?.addEventListener('abort', () => {
            aborted = true;
            rej(new DOMException('cancelled', 'AbortError'));
          }),
        ),
    );
    const { enter, unmount } = setup();
    enter.mockRejectedValue(challengeErr(powChallenge));
    await userEvent.click(await screen.findByRole('button', { name: 'Enter the draw' }));
    await screen.findByText('Verifying you’re human…');
    unmount();
    await waitFor(() => expect(aborted).toBe(true));
  });

  it('an unsupported CAPTCHA provider fails visibly instead of hanging', async () => {
    const { enter } = setup();
    enter.mockRejectedValue(challengeErr({ ...captchaChallenge, captcha: { provider: 'hcaptcha', site_key: 'k' } }));
    await userEvent.click(await screen.findByRole('button', { name: 'Enter the draw' }));
    const panel = await screen.findByText('This check isn’t available here');
    expect(within(panel.closest('div')!.parentElement!).getByText(/hcaptcha/)).toBeInTheDocument();
  });
});
