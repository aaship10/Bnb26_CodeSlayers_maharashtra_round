import { expect, test } from '@playwright/test';
import { unlockAdmin } from './helpers';

test.describe('simulator and evidence', () => {
  test('demo presets 1 and 2, then FCFS vs Fair Drop side by side', async ({ page }) => {
    test.setTimeout(120_000);
    await unlockAdmin(page);
    const ids: string[] = [];
    for (const preset of ['1 · FCFS under attack', '2 · Fair Drop, same attack']) {
      await page.goto('/admin/sim');
      await page.getByRole('button', { name: preset }).click();
      await page.getByRole('button', { name: 'Start run' }).click();
      await expect(page).toHaveURL(/\/admin\/sim\/runs\/run_\d+/);
      ids.push(page.url().split('/').pop()!);
      await expect(page.getByRole('region', { name: 'Fairness' })).toBeVisible({ timeout: 45_000 });
      await expect(page.getByTestId('synthetic-banner')).toBeVisible();
      await expect(page.getByTestId('integrity')).toHaveAttribute('data-passed', 'true');
    }
    await page.goto(`/admin/sim/compare?fcfs=${ids[0]}&lottery=${ids[1]}`);
    const row = page.getByTestId('cmp-Share of seats won by bots');
    await expect(row).toContainText('Fair Drop better');
    await expect(row).toContainText('95% CI');
  });

  test('experiments render charts with CI bands and a table view', async ({ page }) => {
    await unlockAdmin(page);
    await page.goto('/admin/sim/experiments');
    const chart = page.getByTestId('chart-bot_share_vs_request_multiplier');
    await expect(chart.locator('.recharts-line')).toHaveCount(2);
    await expect(chart.locator('.recharts-area')).toHaveCount(2);
    await chart.getByRole('button', { name: 'Show as table' }).click();
    await expect(chart.getByText(/95% CI/).first()).toBeVisible();
  });
});
