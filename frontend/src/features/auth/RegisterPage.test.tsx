// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RegisterPage } from './RegisterPage';
import { api } from '@/api';
import { ApiError } from '@/api/errors';
import { sessionStore } from '@/state/session';
import { deferred, renderRoute } from '@/test/render';

const SESSION = { token: 'tok-1', expires_at: '2026-11-01T11:00:00.000Z', user_id: 'u-1' };

function renderRegister(state?: unknown) {
  return renderRoute(<RegisterPage />, { path: '/register', route: '/register', state });
}

async function fillDetails(user: ReturnType<typeof userEvent.setup>, name = 'Asha', email = 'asha@example.com') {
  await user.type(screen.getByLabelText(/what should we call you/i), name);
  await user.type(screen.getByLabelText(/^email/i), email);
}

beforeEach(() => {
  window.localStorage.clear();
  sessionStore.clear();
});

afterEach(() => vi.restoreAllMocks());

describe('honeypot', () => {
  it('is hidden from people and assistive tech, unreachable by keyboard, and not autofilled', () => {
    renderRegister();
    const hp = document.querySelector<HTMLInputElement>('input[name="hp"]')!;
    expect(hp).toBeInTheDocument();
    expect(hp.tabIndex).toBe(-1);
    expect(hp.getAttribute('autocomplete')).toBe('off');
    expect(hp.value).toBe('');
    expect(hp.closest('[aria-hidden="true"]')).not.toBeNull();
    expect(hp.closest('div')!.style.left).toBe('-10000px');
  });

  it('is always sent, empty for a real person', async () => {
    const register = vi.spyOn(api.auth, 'register').mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderRegister();
    await fillDetails(user);
    await user.click(screen.getByRole('button', { name: /send me a code/i }));
    expect(register).toHaveBeenCalledWith({ email: 'asha@example.com', display_name: 'Asha', hp: '' });
  });

  it('is sent as filled when a bot fills it, so the server can decide', async () => {
    const register = vi.spyOn(api.auth, 'register').mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderRegister();
    await fillDetails(user);
    fireEvent.change(document.querySelector('input[name="hp"]')!, { target: { value: 'http://spam.example' } });
    await user.click(screen.getByRole('button', { name: /send me a code/i }));
    expect(register.mock.calls[0]![0].hp).toBe('http://spam.example');
  });
});

describe('details step', () => {
  it('validates before calling the API, with friendly messages', async () => {
    const register = vi.spyOn(api.auth, 'register');
    const user = userEvent.setup();
    renderRegister();
    await user.click(screen.getByRole('button', { name: /send me a code/i }));
    expect(screen.getByText('Tell us what to call you.')).toBeInTheDocument();
    expect(screen.getByText('That doesn’t look like an email address.')).toBeInTheDocument();
    expect(register).not.toHaveBeenCalled();
    expect(screen.getByLabelText(/^email/i)).toHaveAttribute('aria-invalid', 'true');
  });

  it('a double tap sends one request and disables the button while pending', async () => {
    const d = deferred<undefined>();
    const register = vi.spyOn(api.auth, 'register').mockReturnValue(d.promise);
    const user = userEvent.setup();
    renderRegister();
    await fillDetails(user);
    const btn = screen.getByRole('button', { name: /send me a code/i });
    await user.dblClick(btn);
    expect(register).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: /sending/i })).toBeDisabled();
    d.resolve(undefined);
    expect(await screen.findByRole('heading', { name: /check your email/i })).toBeInTheDocument();
  });

  it('shows a calm message with the wait time when rate limited', async () => {
    vi.spyOn(api.auth, 'register').mockRejectedValue(new ApiError('RATE_LIMITED', 'slow', 429, { retry_after_ms: 6000 }, 6000));
    const user = userEvent.setup();
    renderRegister();
    await fillDetails(user);
    await user.click(screen.getByRole('button', { name: /send me a code/i }));
    expect(await screen.findByText(/let’s slow down/i)).toBeInTheDocument();
    expect(screen.getByText(/try again in 6 seconds/i)).toBeInTheDocument();
  });
});

describe('code step', () => {
  async function reachCodeStep() {
    vi.spyOn(api.auth, 'register').mockResolvedValue(undefined);
    const user = userEvent.setup();
    const utils = renderRegister({ from: '/events/evt_1' });
    await fillDetails(user);
    await user.click(screen.getByRole('button', { name: /send me a code/i }));
    await screen.findByRole('heading', { name: /check your email/i });
    return { user, ...utils };
  }

  it('moves focus to the code field and announces that a code was sent', async () => {
    await reachCodeStep();
    expect(screen.getByLabelText(/one-time code/i)).toHaveFocus();
    expect(screen.getByTestId('announcer')).toHaveTextContent('We sent a code to asha@example.com');
  });

  it('the code field is built for one-time codes on phones', async () => {
    await reachCodeStep();
    const otp = screen.getByLabelText(/one-time code/i);
    expect(otp).toHaveAttribute('inputmode', 'numeric');
    expect(otp).toHaveAttribute('autocomplete', 'one-time-code');
  });

  it('signs in with the right code and returns to where the person came from', async () => {
    const verify = vi.spyOn(api.auth, 'verify').mockResolvedValue(SESSION);
    const { user } = await reachCodeStep();
    await user.type(screen.getByLabelText(/one-time code/i), '123456');
    await user.click(screen.getByRole('button', { name: /confirm and continue/i }));

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/events/evt_1'));
    expect(verify).toHaveBeenCalledWith({ email: 'asha@example.com', otp: '123456' });
    expect(sessionStore.getSnapshot()).toMatchObject({ token: 'tok-1', user_id: 'u-1' });
  });

  it('never puts the token or email in the URL', async () => {
    vi.spyOn(api.auth, 'verify').mockResolvedValue(SESSION);
    const { user } = await reachCodeStep();
    await user.type(screen.getByLabelText(/one-time code/i), '123456');
    await user.click(screen.getByRole('button', { name: /confirm and continue/i }));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/events/evt_1'));
    expect(screen.getByTestId('location').textContent).not.toMatch(/tok-1|asha|@/);
  });

  it('refuses an open redirect smuggled through the "from" state', async () => {
    vi.spyOn(api.auth, 'register').mockResolvedValue(undefined);
    vi.spyOn(api.auth, 'verify').mockResolvedValue(SESSION);
    const user = userEvent.setup();
    renderRegister({ from: '//evil.example/phish' });
    await fillDetails(user);
    await user.click(screen.getByRole('button', { name: /send me a code/i }));
    await user.type(await screen.findByLabelText(/one-time code/i), '123456');
    await user.click(screen.getByRole('button', { name: /confirm and continue/i }));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/$/));
  });

  it('a wrong code is explained next to the field and nothing is stored', async () => {
    vi.spyOn(api.auth, 'verify').mockRejectedValue(new ApiError('VALIDATION_ERROR', 'That code is not right', 400));
    const { user } = await reachCodeStep();
    await user.type(screen.getByLabelText(/one-time code/i), '000000');
    await user.click(screen.getByRole('button', { name: /confirm and continue/i }));
    expect(await screen.findByText('That code is not right')).toBeInTheDocument();
    expect(sessionStore.getSnapshot()).toBeNull();
    expect(screen.getByLabelText(/one-time code/i)).toHaveAttribute('aria-invalid', 'true');
  });

  it('resend is on a 30 s cooldown, and "different email" goes back', async () => {
    const { user } = await reachCodeStep();
    expect(screen.getByRole('button', { name: /resend code in 30s/i })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: /use a different email/i }));
    expect(screen.getByRole('heading', { name: /get your code/i })).toBeInTheDocument();
  });

  it('cannot confirm an empty code', async () => {
    await reachCodeStep();
    expect(screen.getByRole('button', { name: /confirm and continue/i })).toBeDisabled();
  });
});

describe('already signed in', () => {
  it('does not show the form again', () => {
    sessionStore.set(SESSION);
    renderRegister({ from: '/events/evt_1' });
    expect(screen.getByText('You’re signed in')).toBeInTheDocument();
    expect(screen.queryByLabelText(/^email/i)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /carry on/i })).toHaveAttribute('href', '/events/evt_1');
  });
});
