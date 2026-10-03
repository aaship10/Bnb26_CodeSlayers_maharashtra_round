import { expect, test } from '@playwright/test';
import { EVT, mock, scenario, signUp } from './helpers';

test.describe('attendee', () => {
  test('register → enter → refresh → status → claim → ticket', async ({ page }) => {
    await scenario(page, 'window-open');
    await page.goto(EVT);
    await page.getByRole('link', { name: 'Sign in to enter' }).click();
    await expect(page).toHaveURL('/register');
    await page.getByLabel('What should we call you?').fill('Asha');
    await page.getByLabel('Email').fill(`e2e-journey-${Date.now()}@example.com`);
    await page.getByRole('button', { name: 'Send me a code' }).click();
    await page.getByLabel('One-time code').fill('123456');
    await page.getByRole('button', { name: 'Confirm and continue' }).click();
    await expect(page).toHaveURL(EVT); // back where they started

    await page.getByRole('button', { name: 'Enter the draw' }).click();
    await expect(page.getByText('You’re in the draw')).toBeVisible();
    await page.reload();
    await expect(page.getByText('You’re in the draw')).toBeVisible(); // state from the server, not the tab

    await page.getByRole('link', { name: 'See my status' }).click();
    await expect(page.getByRole('heading', { name: 'You’re in the draw' })).toBeVisible();
    await expect(page.getByTestId('live-indicator')).toHaveAttribute('data-connection', 'live');

    // The draw happens while the page is open: no reload needed.
    await mock(page, '/clock', { set: '2026-11-01T10:30:02.000Z' });
    await expect(page.getByRole('heading', { name: 'The draw is running' })).toBeVisible();
    await mock(page, '/clock', { set: '2026-11-01T10:30:12.000Z' });
    await expect(page.getByRole('heading', { name: 'You’ve been picked!' })).toBeVisible();

    await page.getByRole('link', { name: 'Claim my seat' }).click();
    const claim = page.waitForRequest((r) => r.method() === 'POST' && r.url().endsWith('/claim'));
    await page.getByRole('button', { name: 'Claim my seat' }).click();
    expect((await claim).headers()['idempotency-key']).toMatch(/^[0-9a-f-]{36}$/);
    await expect(page).toHaveURL(`${EVT}/ticket`);
    await expect(page.getByTestId('ticket-code')).toHaveText(/^FD-/);
    await page.reload();
    await expect(page.getByTestId('ticket-code')).toHaveText(/^FD-/);
  });

  test('a claim interrupted by a dead network finishes after a refresh with the SAME Idempotency-Key', async ({ page }) => {
    await scenario(page, 'won-hold');
    await signUp(page);
    await page.goto(`${EVT}/claim`);
    const keys: string[] = [];
    page.on('request', (r) => r.method() === 'POST' && r.url().endsWith('/claim') && keys.push(r.headers()['idempotency-key'] ?? ''));
    await page.route('**/api/events/*/claim', (r) => r.abort('connectionreset'));
    await page.getByRole('button', { name: 'Claim my seat' }).click();
    await expect(page.getByText('We’re not sure your claim went through')).toBeVisible({ timeout: 40_000 });
    await page.unroute('**/api/events/*/claim');
    await page.reload();
    await expect(page.getByText('You started claiming earlier')).toBeVisible();
    await page.getByRole('button', { name: 'Finish claiming' }).click();
    await expect(page).toHaveURL(`${EVT}/ticket`);
    expect(new Set(keys).size).toBe(1);
    expect(keys.length).toBeGreaterThanOrEqual(2);
  });

  test('429 on enter: countdown, no automatic re-fire, then recovery', async ({ page }) => {
    await scenario(page, 'enter-rate-limited');
    await signUp(page);
    await page.goto(EVT);
    let calls = 0;
    page.on('request', (r) => r.method() === 'POST' && r.url().endsWith('/enter') && calls++);
    await page.getByRole('button', { name: 'Enter the draw' }).click();
    await expect(page.getByText('Let’s slow down a little')).toBeVisible();
    await expect(page.getByRole('button', { name: /Try again in \d+s/ })).toBeDisabled();
    await page.waitForTimeout(1500);
    expect(calls).toBe(1);
    await page.getByRole('button', { name: 'Try again', exact: true }).click({ timeout: 15_000 });
    await expect(page.getByText('You’re in the draw')).toBeVisible();
  });

  test('proof-of-work challenge is solved in a worker and the same request is repeated', async ({ page }) => {
    await scenario(page, 'enter-challenge-pow');
    await signUp(page);
    await page.goto(EVT);
    const enters: Record<string, string>[] = [];
    page.on('request', (r) => r.method() === 'POST' && r.url().endsWith('/enter') && enters.push(r.headers()));
    await page.getByRole('button', { name: 'Enter the draw' }).click();
    await expect(page.getByText('You’re in the draw')).toBeVisible({ timeout: 30_000 });
    expect(enters).toHaveLength(2);
    expect(enters[0]!['x-challenge-id']).toBeUndefined();
    expect(enters[1]!['x-challenge-id']).toBeTruthy();
    expect(enters[1]!['x-challenge-solution']).toMatch(/^\d+$/);
  });

  test('CAPTCHA challenge then success', async ({ page }) => {
    await scenario(page, 'enter-challenge-captcha');
    await signUp(page);
    await page.goto(EVT);
    await page.getByRole('button', { name: 'Enter the draw' }).click();
    await expect(page.getByText(/Can’t use this check/)).toBeVisible();
    await page.getByRole('checkbox', { name: /I’m a person/ }).click();
    await expect(page.getByText('You’re in the draw')).toBeVisible();
  });

  test('SSE drop: shows reconnecting, resumes with Last-Event-ID, back to live', async ({ page }) => {
    await scenario(page, 'entered-waiting');
    await signUp(page);
    await page.goto(`${EVT}/status`);
    await expect(page.getByTestId('live-indicator')).toHaveAttribute('data-connection', 'live');
    await mock(page, '/clock', { set: '2026-11-01T10:30:02.000Z' }); // an event, so there's an id to resume from
    await expect(page.getByRole('heading', { name: 'The draw is running' })).toBeVisible();
    const reconnect = page.waitForRequest((r) => r.url().endsWith('/stream'));
    await mock(page, '/sse/drop');
    await expect(page.getByTestId('live-indicator')).toHaveAttribute('data-connection', 'reconnecting');
    expect((await reconnect).headers()['last-event-id']).toBeTruthy();
    await expect(page.getByTestId('live-indicator')).toHaveAttribute('data-connection', 'live');
  });

  test('no live stream at all: falls back to polite polling and still follows the story', async ({ page }) => {
    await scenario(page, 'no-sse');
    await signUp(page);
    await page.goto(`${EVT}/status`);
    await expect(page.getByTestId('live-indicator')).toHaveAttribute('data-connection', 'polling', { timeout: 20_000 });
    await mock(page, '/clock', { set: '2026-11-01T10:30:02.000Z' });
    await expect(page.getByRole('heading', { name: 'The draw is running' })).toBeVisible({ timeout: 15_000 });
  });
});
