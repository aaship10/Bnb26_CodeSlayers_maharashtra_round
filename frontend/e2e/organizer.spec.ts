import { expect, test } from '@playwright/test';
import { mock, scenario, unlockAdmin } from './helpers';

test.describe('organizer', () => {
  test('token gate: wrong token refused, right token unlocks, kept in sessionStorage only', async ({ page }) => {
    await page.goto('/admin');
    await page.getByLabel('Admin token').fill('wrong');
    await page.getByRole('button', { name: 'Unlock' }).click();
    await expect(page.getByText(/wasn’t accepted/)).toBeVisible();
    await page.getByLabel('Admin token').fill('dev-admin-token');
    await page.getByRole('button', { name: 'Unlock' }).click();
    await expect(page.getByRole('heading', { name: 'Events' })).toBeVisible();
    expect(await page.evaluate(() => sessionStorage.getItem('fd.admin_token'))).toBe('dev-admin-token');
    expect(await page.evaluate(() => JSON.stringify({ ...localStorage }))).not.toContain('dev-admin-token');
  });

  test('lifecycle: create → schedule → open → close → draw, each confirmed, each with an Idempotency-Key', async ({ page }) => {
    await scenario(page, 'fresh');
    await unlockAdmin(page);
    await page.goto('/admin');
    await page.getByRole('button', { name: 'New event' }).click();
    await page.getByLabel('Name').fill('E2E rehearsal');
    await page.getByRole('button', { name: 'Create as draft' }).click();
    await expect(page).toHaveURL(/\/admin\/events\/evt_/);

    const keys: string[] = [];
    page.on('request', (r) => r.method() === 'POST' && /\/(schedule|open|close|draw)$/.test(r.url()) && keys.push(r.headers()['idempotency-key'] ?? ''));
    for (const [button, title, next] of [
      ['Schedule', 'Publish this event?', 'Scheduled'],
      ['Open window', 'Open the entry window now?', 'Open'],
      ['Close window', 'Close the entry window?', 'Drawing'],
      ['Run draw', 'Run the draw?', 'Claiming'],
    ] as const) {
      await page.getByRole('button', { name: button, exact: true }).click();
      const dialog = page.getByRole('dialog', { name: title });
      await dialog.getByRole('button', { name: button === 'Schedule' ? 'Publish' : button }).click();
      await expect(dialog).toBeHidden();
      await expect(page.locator('[aria-current="step"]')).toContainText(next);
    }
    expect(keys).toHaveLength(4);
    expect(new Set(keys).size).toBe(4);
    for (const k of keys) expect(k).toMatch(/^[0-9a-f-]{36}$/);
  });

  test('invariants badge turns red when an invariant breaks', async ({ page }) => {
    await scenario(page, 'won-hold');
    await unlockAdmin(page);
    await page.goto('/admin/events/evt_demo_01');
    await expect(page.getByTestId('invariants')).toHaveAttribute('data-state', 'ok');
    await mock(page, '/invariants', { broken: true });
    await expect(page.getByTestId('invariants')).toHaveAttribute('data-state', 'bad', { timeout: 20_000 });
    await expect(page.getByText('An allocation invariant is violated')).toBeVisible();
    await mock(page, '/invariants', { broken: false });
  });
});
