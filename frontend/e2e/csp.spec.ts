import { expect, test } from '@playwright/test';
import { EVT, scenario, unlockAdmin } from './helpers';

/**
 * Under the production CSP (nginx deploy) the app must not trip a single
 * policy violation: no inline scripts, no eval, workers from 'self'.
 * Against `vite preview` (no CSP header) this simply finds nothing.
 */
test('no Content-Security-Policy violations on the main screens', async ({ page }) => {
  const violations: string[] = [];
  page.on('console', (m) => /Content Security Policy|Refused to/i.test(m.text()) && violations.push(m.text()));
  await page.addInitScript(() => document.addEventListener('securitypolicyviolation', (e) => console.error(`Refused to load: ${e.violatedDirective} ${e.blockedURI}`)));
  await scenario(page, 'won-hold');
  for (const path of ['/', EVT, `${EVT}/fairness`]) {
    await page.goto(path);
    await page.waitForLoadState('domcontentloaded');
  }
  // Workers are where CSP usually bites: run the verifier.
  await page.getByRole('button', { name: 'Verify the draw' }).click();
  await expect(page.getByText('Verified: this draw is exactly reproducible')).toBeVisible({ timeout: 30_000 });
  await unlockAdmin(page);
  await page.goto('/admin/sim/experiments');
  await expect(page.locator('svg.recharts-surface[role="application"]').first()).toBeVisible();
  expect(violations).toEqual([]);
});
