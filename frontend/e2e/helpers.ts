import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

export const EVT = '/events/evt_demo_01';

/**
 * Drive the mock server's control API on its own port, so the suite also runs
 * through nginx (which deliberately blocks /__mock).
 */
export const MOCK_URL = process.env.MOCK_URL ?? 'http://127.0.0.1:8787';

export async function mock(page: Page, path: string, body: unknown = {}) {
  const res = await page.request.post(`${MOCK_URL}/__mock${path}`, { data: body });
  expect(res.ok(), `mock ${path}`).toBeTruthy();
  return res.json();
}

export const scenario = (page: Page, id: string) => mock(page, '/scenario', { id });

let counter = 0;
/** Sign up a fresh person through the real UI (code 123456 on the mock). */
export async function signUp(page: Page, name = 'Asha') {
  await page.goto('/register');
  await page.getByLabel('What should we call you?').fill(name);
  await page.getByLabel('Email').fill(`e2e-${Date.now()}-${++counter}@example.com`);
  await page.getByRole('button', { name: 'Send me a code' }).click();
  await page.getByLabel('One-time code').fill('123456');
  await page.getByRole('button', { name: 'Confirm and continue' }).click();
  await expect(page).toHaveURL('/');
}

export async function unlockAdmin(page: Page) {
  await page.goto('/');
  await page.evaluate(() => sessionStorage.setItem('fd.admin_token', 'dev-admin-token'));
}

/**
 * No serious or critical accessibility violations. Smaller findings are
 * attached to the report rather than failing the run.
 */
export async function expectAccessible(page: Page, label: string) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
  const bad = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
  const summary = bad.map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(' ')).slice(0, 3).join(' | ')}`);
  const minor = results.violations.filter((v) => v.impact !== 'serious' && v.impact !== 'critical');
  if (minor.length) test.info().annotations.push({ type: `axe minor (${label})`, description: minor.map((v) => `${v.id}:${v.impact}`).join(', ') });
  expect(summary, `${label}: serious/critical axe violations`).toEqual([]);
  return results.violations;
}
