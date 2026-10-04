import { expect, test } from '@playwright/test';
import { EVT, mock, scenario } from './helpers';

test.describe('fairness', () => {
  test('the 50,000-entrant draw verifies in the browser, in a worker, in a few seconds', async ({ page }) => {
    await scenario(page, 'won-hold');
    await page.goto(`${EVT}/fairness`);
    const workers: string[] = [];
    page.on('worker', (w) => workers.push(w.url()));
    const t0 = Date.now();
    await page.getByRole('button', { name: 'Verify the draw' }).click();
    await expect(page.getByText('Verified: this draw is exactly reproducible')).toBeVisible({ timeout: 30_000 });
    const ms = Date.now() - t0;
    test.info().annotations.push({ type: 'verify time', description: `${ms} ms` });
    expect(ms).toBeLessThan(10_000);
    expect(workers.some((u) => /verify\.worker/.test(u))).toBe(true);
    for (const step of ['commitment', 'entrants', 'seed', 'rank', 'results']) await expect(page.getByTestId(`step-${step}`)).toHaveAttribute('data-status', 'pass');
    await page.getByRole('button', { name: 'Try the first winner’s ID' }).click();
    await expect(page.getByText('Picked: winner #1 of 500')).toBeVisible();
  });

  test('a tampered entrant list fails at step 2 with a clear message', async ({ page }) => {
    await scenario(page, 'won-hold');
    await mock(page, '/tamper', { mode: 'entrants' });
    await page.goto(`${EVT}/fairness`);
    await page.getByRole('button', { name: 'Verify the draw' }).click();
    await expect(page.getByText('This draw does not match its public record')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId('step-entrants')).toHaveAttribute('data-status', 'fail');
    await expect(page.getByTestId('step-entrants')).toContainText('The list has changed since the window closed');
    await mock(page, '/tamper', { mode: 'none' });
  });

  test('the audit chain verifies; an edited record is caught even though the server says ok', async ({ page }) => {
    await scenario(page, 'won-hold');
    await page.goto(`${EVT}/audit`);
    await page.getByRole('button', { name: 'Check the chain in my browser' }).click();
    await expect(page.getByText('Chain intact')).toBeVisible();
    await mock(page, '/tamper', { mode: 'audit' });
    await page.reload();
    await page.getByRole('button', { name: 'Check the chain in my browser' }).click();
    await expect(page.getByText(/don’t take its word for it/)).toBeVisible();
    await mock(page, '/tamper', { mode: 'none' });
  });
});
