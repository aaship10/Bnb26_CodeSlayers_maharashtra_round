import { expect, test } from '@playwright/test';
import { EVT, expectAccessible, scenario, signUp, unlockAdmin } from './helpers';

/** axe (WCAG 2.1 A/AA) on every main screen, desktop and phone. Serious or critical findings fail the run. */
test.describe('accessibility', () => {
  test('attendee screens', async ({ page }) => {
    await scenario(page, 'window-open');
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Drops' })).toBeVisible();
    await expectAccessible(page, 'home');

    await page.goto('/register');
    await expectAccessible(page, 'register');

    await signUp(page);
    await page.goto(EVT);
    await expect(page.getByRole('button', { name: 'Enter the draw' })).toBeVisible();
    await expectAccessible(page, 'event page');

    await scenario(page, 'won-hold');
    await page.goto(`${EVT}/status`);
    await expect(page.getByRole('heading', { name: 'You’ve been picked!' })).toBeVisible();
    await expectAccessible(page, 'status (won)');

    await page.goto(`${EVT}/claim`);
    await expect(page.getByRole('button', { name: 'Claim my seat' })).toBeVisible();
    await expectAccessible(page, 'claim');
    await page.getByRole('button', { name: 'Claim my seat' }).click();
    await expect(page.getByTestId('ticket-code')).toBeVisible();
    await expectAccessible(page, 'ticket');
  });

  test('fairness and audit', async ({ page }) => {
    await scenario(page, 'won-hold');
    await page.goto(`${EVT}/fairness`);
    await page.getByRole('button', { name: 'Verify the draw' }).click();
    await expect(page.getByText('Verified: this draw is exactly reproducible')).toBeVisible({ timeout: 30_000 });
    await expectAccessible(page, 'fairness (verified)');
    await page.goto(`${EVT}/audit`);
    await expect(page.getByText('DRAW_COMPLETED')).toBeVisible();
    await expectAccessible(page, 'audit');
  });

  test('organizer screens', async ({ page }) => {
    await scenario(page, 'won-hold');
    await page.goto('/admin');
    await expect(page.getByRole('dialog', { name: 'Organizer access' })).toBeVisible();
    await expectAccessible(page, 'unlock dialog');
    await unlockAdmin(page);
    await page.goto('/admin/events/evt_demo_01');
    await expect(page.getByText('People by state')).toBeVisible();
    await expectAccessible(page, 'admin event');
    await page.goto('/admin/sim');
    await expect(page.getByRole('button', { name: 'Start run' })).toBeVisible();
    await expectAccessible(page, 'simulator');
    await page.goto('/admin/sim/experiments');
    await expect(page.locator('svg.recharts-surface[role="application"]').first()).toBeVisible(); // a flat line has a zero-height box, so wait on the surface
    await expectAccessible(page, 'experiments');
  });
});
