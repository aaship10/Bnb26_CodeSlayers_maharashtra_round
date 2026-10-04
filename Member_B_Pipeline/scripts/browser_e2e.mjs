/**
 * Real-browser end-to-end through the gateway: Member D's built UI + Member B's real backend.
 * Uses the locally installed Chrome (no browser download) via the frontend's playwright-core.
 *
 *   (cd ../frontend && npm run build)           # once: the gateway serves frontend/dist
 *   python infra/local/run.py up
 *   python infra/local/run.py exec -- node scripts/browser_e2e.mjs
 *
 * Screenshots land in .local/screens/. Exit code 1 if any check fails.
 */
import { createRequire } from 'node:module';
import { mkdirSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const require = createRequire(join(root, '..', 'frontend', 'package.json'));
const { chromium } = require('playwright-core');

const BASE = process.env.E2E_BASE ?? 'http://127.0.0.1:8080';
const OUTBOX = process.env.OUTBOX_DIR ?? join(root, '.local', 'outbox');
const SHOTS = join(root, '.local', 'screens');
const EVENT = '11111111-1111-1111-1111-111111111111';
const ADMIN = process.env.ADMIN_TOKEN ?? '';
mkdirSync(SHOTS, { recursive: true });

const results = [];
const check = (name, ok, detail = '') => {
  results.push({ name, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${!ok && detail ? `  [${detail}]` : ''}`);
};

function otpFor(email, since) {
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) {
    const files = readdirSync(OUTBOX)
      .map((f) => join(OUTBOX, f))
      .filter((f) => statSync(f).mtimeMs >= since - 1000)
      .sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs);
    for (const f of files) {
      const m = JSON.parse(readFileSync(f, 'utf8'));
      if (m.to === email) return /(\d{6})/.exec(m.body)[1];
    }
  }
  throw new Error(`no OTP mail for ${email} in ${OUTBOX}: is the stack sending real email? restart with: fd.ps1 up -Mail file`);
}

async function setPreset(preset) {
  if (!ADMIN) return;
  const defences = typeof preset === 'string' ? { preset } : preset;
  const r = await fetch(`${BASE}/api/admin/events/${EVENT}/config`, {
    method: 'PATCH',
    headers: { 'X-Admin-Token': ADMIN, 'Content-Type': 'application/json' },
    body: JSON.stringify({ defences }),
  });
  if (!r.ok) throw new Error(`PATCH config failed: ${r.status} ${await r.text()}`);
  await new Promise((r) => setTimeout(r, 2200)); // replica config cache
}

/** Sign a brand-new person in through the real UI. Returns { userId, email }. */
async function signIn(page, label) {
  const email = `${label}.${Math.random().toString(16).slice(2, 8)}@example-college.edu`;
  let userId = null;
  page.on('response', async (r) => {
    if (r.url().endsWith('/api/auth/verify') && r.status() === 200) userId = (await r.json()).user_id;
  });
  await page.goto(`${BASE}/register`);
  const since = Date.now();
  await page.getByLabel('What should we call you?').fill(label);
  await page.getByLabel('Email').fill(email);
  await page.getByRole('button', { name: /Send me a code/i }).click();
  await page.getByRole('heading', { name: /Check your email/i }).waitFor({ timeout: 10000 });
  await page.getByLabel('One-time code').fill(otpFor(email, since));
  await page.getByRole('button', { name: /Confirm and continue/i }).click();
  await page.waitForURL((u) => !u.pathname.startsWith('/register'), { timeout: 10000 });
  for (let i = 0; i < 50 && !userId; i++) await new Promise((r) => setTimeout(r, 100));
  return { userId, email };
}

/** Record the enter requests the page makes: [{status, challengeHeaders}] */
function watchEnter(page) {
  const seen = [];
  page.on('requestfinished', async (req) => {
    if (req.method() === 'POST' && /\/api\/events\/[^/]+\/enter$/.test(req.url())) {
      const res = await req.response();
      const h = await req.allHeaders();
      seen.push({ status: res.status(), withSolution: !!(h['x-challenge-id'] && h['x-challenge-solution']) });
    }
  });
  return seen;
}

const browser = await chromium.launch({ channel: 'chrome', headless: true });
try {
  await setPreset('none');
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 900 } });
  const page = await ctx.newPage();
  const consoleErrors = [];
  const badResponses = [];
  page.on('console', (m) => m.type() === 'error' && consoleErrors.push(m.text()));
  const clientErrors = [];
  page.on('response', (r) => {
    const u = new URL(r.url());
    if (!u.pathname.startsWith('/api/')) return;
    if (r.status() >= 500) badResponses.push(`${r.status()} ${u.pathname}`);
    else if (r.status() >= 400) clientErrors.push(`${r.status()} ${r.request().method()} ${u.pathname.replace(/[0-9a-f-]{36}/g, '<id>')}`);
  });

  // 1. the app loads from the gateway and lists the drop
  await page.goto(BASE + '/');
  await page.getByText('Fair Drop demo event').first().waitFor({ timeout: 10000 });
  check('home page lists the event from the real API', true);

  // 2. signed out: the event page asks to sign in
  await page.goto(`${BASE}/events/${EVENT}`);
  await page.getByRole('link', { name: /Sign in to enter/i }).waitFor({ timeout: 10000 });
  check('signed-out event page offers "Sign in to enter"', true);

  // 3. the honeypot field exists but is invisible and empty
  await page.goto(`${BASE}/register`);
  const hp = page.locator('input[name="hp"]');
  // Humans cannot see or reach it: parked off-screen, aria-hidden, out of the tab order.
  const box = await hp.boundingBox();
  const hidden = await hp.evaluate((el) => el.closest('[aria-hidden="true"]') !== null && el.tabIndex === -1);
  check('honeypot input present, off-screen, aria-hidden, untabbable, empty', (await hp.count()) === 1 && hidden && box !== null && box.x < 0 && (await hp.inputValue()) === '');

  // 4. sign up with a real OTP delivered by the backend
  const email = `browser.${Math.random().toString(16).slice(2, 8)}@example-college.edu`;
  const since = Date.now();
  await page.getByLabel('What should we call you?').fill('Browser Tester');
  await page.getByLabel('Email').fill(email);
  await page.getByRole('button', { name: /Send me a code/i }).click();
  await page.getByRole('heading', { name: /Check your email/i }).waitFor({ timeout: 10000 });
  check('register -> "Check your email" step', true);
  await page.screenshot({ path: join(SHOTS, '1-code-step.png') });

  // wrong code first: the UI must show an error, not sign in
  await page.getByLabel('One-time code').fill('000000');
  await page.getByRole('button', { name: /Confirm and continue/i }).click();
  await page.getByRole('alert').first().waitFor({ timeout: 8000 }).catch(() => {});
  check('wrong code does not sign in', page.url().includes('/register'));

  await page.getByLabel('One-time code').fill(otpFor(email, since));
  await page.getByRole('button', { name: /Confirm and continue/i }).click();
  await page.waitForURL((u) => !u.pathname.startsWith('/register'), { timeout: 10000 });
  check('correct code signs in and leaves /register', true);

  // 5. session survives a reload
  await page.goto(`${BASE}/events/${EVENT}`);
  await page.getByRole('button', { name: /Enter the draw/i }).waitFor({ timeout: 10000 });
  await page.reload();
  await page.getByRole('button', { name: /Enter the draw/i }).waitFor({ timeout: 10000 });
  check('session survives a page reload ("Enter the draw" visible)', true);

  // 6. enter the draw, with defences OFF
  await page.getByRole('button', { name: /Enter the draw/i }).click();
  await page.getByText(/You’re in the draw/).waitFor({ timeout: 10000 });
  check('enter works (preset none)', true);
  await page.screenshot({ path: join(SHOTS, '2-entered.png') });

  // 7. entry persists across reload (state comes from the server, not the browser)
  await page.reload();
  await page.getByText(/You’re in the draw|You took part/).first().waitFor({ timeout: 10000 });
  check('entered state is restored from the server after reload', true);

  // 8. the status page works for the real user
  // The page now subscribes to the live status stream (SSE), a connection that never goes idle, so wait for
  // the stream itself rather than "network idle".
  const streamResponse = page.waitForResponse((r) => /\/api\/events\/[^/]+\/stream$/.test(r.url()), { timeout: 15000 });
  await page.goto(`${BASE}/events/${EVENT}/status`);
  const sr = await streamResponse;
  check('status page opens the live SSE stream through the gateway (200, text/event-stream)',
    sr.status() === 200 && (sr.headers()['content-type'] ?? '').includes('text/event-stream'), `${sr.status()} ${sr.headers()['content-type']}`);
  await page.waitForTimeout(1500);
  check('status page loads without a 5xx', badResponses.length === 0, badResponses.join(', '));
  await page.screenshot({ path: join(SHOTS, '3-status.png') });

  // 9. a second browser (new device id) signing in as the same email lands on the same identity
  const ctx2 = await browser.newContext();
  const p2 = await ctx2.newPage();
  await p2.goto(`${BASE}/register`);
  const since2 = Date.now();
  await p2.getByLabel('What should we call you?').fill('Other Device');
  await p2.getByLabel('Email').fill(email.replace('@', '+second@'));
  await p2.getByRole('button', { name: /Send me a code/i }).click();
  await p2.getByRole('heading', { name: /Check your email/i }).waitFor({ timeout: 10000 });
  await p2.getByLabel('One-time code').fill(otpFor(email.replace('@', '+second@'), since2));
  await p2.getByRole('button', { name: /Confirm and continue/i }).click();
  await p2.waitForURL((u) => !u.pathname.startsWith('/register'), { timeout: 10000 });
  await p2.goto(`${BASE}/events/${EVENT}`);
  await p2.getByText(/You’re in the draw|You took part|You had already entered/).first().waitFor({ timeout: 10000 });
  check('a +tag alias on another device is the SAME identity (already entered)', true);

  // ---- stage 4: challenges, driven by the real UI ------------------------------------------------
  const gotoEvent = async (pg) => {
    await pg.goto(`${BASE}/events/${EVENT}`);
    await pg.getByRole('button', { name: /Enter the draw/i }).waitFor({ timeout: 10000 });
  };
  const entered = (pg) => pg.getByText(/You’re in the draw/).waitFor({ timeout: 20000 });

  // A. proof-of-work only: the browser's worker solves it with no interaction
  await setPreset('rate_limit+pow');
  const pa = await (await browser.newContext()).newPage();
  const enterA = watchEnter(pa);
  await signIn(pa, 'powuser');
  await gotoEvent(pa);
  await pa.getByRole('button', { name: /Enter the draw/i }).click();
  await entered(pa);
  check('PoW: first /enter is 403 CHALLENGE_REQUIRED, the retry carries the solution and succeeds',
    enterA.length === 2 && enterA[0].status === 403 && !enterA[0].withSolution && enterA[1].status === 201 && enterA[1].withSolution,
    JSON.stringify(enterA));
  await pa.screenshot({ path: join(SHOTS, '4-pow-entered.png') });

  // B. proof-of-work + CAPTCHA: the person clicks the widget once
  await setPreset('rate_limit+pow+captcha');
  const pb = await (await browser.newContext()).newPage();
  const enterB = watchEnter(pb);
  await signIn(pb, 'captchauser');
  await gotoEvent(pb);
  await pb.getByRole('button', { name: /Enter the draw/i }).click();
  const captchaBox = pb.getByRole('checkbox', { name: /I’m a person, not a bot/i });
  await captchaBox.waitFor({ timeout: 15000 });
  await pb.screenshot({ path: join(SHOTS, '5-captcha-shown.png') });
  await captchaBox.click();
  await entered(pb);
  check('PoW + CAPTCHA: three /enter calls (pow challenge, captcha challenge, success)',
    enterB.map((e) => e.status).join(',') === '403,403,201' && enterB[2].withSolution, JSON.stringify(enterB));

  // C. accessible fallback: an organiser waives the CAPTCHA for one person; PoW still runs automatically
  const pc = await (await browser.newContext()).newPage();
  const enterC = watchEnter(pc);
  const { userId } = await signIn(pc, 'waived');
  if (ADMIN && userId) {
    const w = await fetch(`${BASE}/api/admin/defence/waivers`, {
      method: 'POST', headers: { 'X-Admin-Token': ADMIN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ event_id: EVENT, user_id: userId, reason: 'browser e2e: screen reader user' }),
    });
    check('organiser can waive the CAPTCHA for one person', w.status === 201, String(w.status));
    await gotoEvent(pc);
    await pc.getByRole('button', { name: /Enter the draw/i }).click();
    await entered(pc);
    const sawCaptcha = await pc.getByRole('checkbox', { name: /I’m a person/i }).count();
    check('waived person enters with PoW only (no CAPTCHA step)', sawCaptcha === 0 && enterC.map((e) => e.status).join(',') === '403,201', JSON.stringify(enterC));
  }

  // D. a harder puzzle: 20 bits in the real browser worker, timed
  await setPreset({ preset: 'custom', layers: { pow: { enabled: true, mode: 'always', base_bits: 20, max_bits: 22 } } });
  const pd = await (await browser.newContext()).newPage();
  await signIn(pd, 'hard');
  await gotoEvent(pd);
  const t0 = Date.now();
  await pd.getByRole('button', { name: /Enter the draw/i }).click();
  await entered(pd);
  const ms = Date.now() - t0;
  console.log(`      20-bit proof-of-work solved by D's worker in real Chrome, click -> entered: ${ms} ms`);
  check('a 20-bit puzzle completes in the browser in under 30 s', ms < 30000, `${ms} ms`);
  await setPreset('none');

  // ---- stage 5: risk engine in the loop (preset "all": PoW for everyone, CAPTCHA only for risky entrants) -------
  await setPreset('all');
  const pe = await (await browser.newContext()).newPage();
  const enterE = watchEnter(pe);
  const { userId: userE } = await signIn(pe, 'riskcheck');
  await gotoEvent(pe);
  await pe.getByRole('button', { name: /Enter the draw/i }).click();
  // A brand-new account that read its code within a second, in a headless browser, is genuinely
  // bot-like, so the risk engine may add a CAPTCHA. Either way the person gets in; click it if shown.
  const captchaE = pe.getByRole('checkbox', { name: /I’m a person, not a bot/i });
  await Promise.race([entered(pe), captchaE.waitFor({ timeout: 20000 })]);
  if (await captchaE.count()) await captchaE.click();
  await entered(pe);
  check('preset all: a risky-looking new account can still always enter', enterE.at(-1)?.status === 201, JSON.stringify(enterE));
  if (ADMIN && userE) {
    // The log is written in batches (<= 0.5 s) by whichever replica served each request, so wait a moment, and
    // order by timestamp: row ids follow INSERT order, which interleaves across replicas.
    await new Promise((r) => setTimeout(r, 1500));
    const res = await fetch(`${BASE}/api/admin/defence/decisions?event_id=${EVENT}&limit=500`, { headers: { 'X-Admin-Token': ADMIN } });
    const mine = (await res.json()).decisions.filter((d) => d.user_id === userE).sort((a, b) => a.ts.localeCompare(b.ts) || a.id - b.id);
    const allow = mine.find((d) => d.action === 'ALLOW');
    const names = (allow?.signals?.signals ?? []).map((x) => x.name);
    check("decision log has this person's CHALLENGE(s) then ALLOW, with an explainable risk breakdown",
      mine.length >= 2 && mine[0].action === 'CHALLENGE' && !!allow && [1, 0.5, 0.25].includes(allow.weight) && typeof allow.score === 'number'
        && names.includes('account_age') && names.includes('otp_latency'),
      JSON.stringify(mine.map((d) => [d.action, d.layer, d.weight, d.score])) + ' ' + names.join(','));
    console.log(`      risk for this new, fast-OTP, headless-browser account: score ${allow?.score}, band ${allow?.signals?.band}, weight ${allow?.weight}, signals: ${names.join(', ')}`);
  }
  await setPreset('none');

  // ---- stage 6: the ops dashboard, rendered by a real browser against the live stack -------------------------------
  if (ADMIN) {
    const wrong = await (await browser.newContext()).newPage();
    await wrong.goto(`${BASE}/ops/`);
    await wrong.getByLabel('Admin token').fill('definitely-wrong');
    await wrong.getByRole('button', { name: 'Connect' }).click();
    await wrong.getByText(/Wrong or missing admin token/).waitFor({ timeout: 10000 });
    check('ops dashboard refuses a wrong admin token', true);

    const po = await (await browser.newContext({ viewport: { width: 1100, height: 1250 } })).newPage();
    const pageErrors = [];
    po.on('pageerror', (e) => pageErrors.push(String(e)));
    po.on('console', (m) => m.type() === 'error' && pageErrors.push(m.text()));
    await po.goto(`${BASE}/ops/`);
    await po.getByLabel('Admin token').fill(ADMIN);
    await po.getByRole('button', { name: 'Connect' }).click();
    await po.getByText(/Live ·/).waitFor({ timeout: 10000 });
    for (let i = 0; i < 40; i++) await fetch(`${BASE}/api/events/${EVENT}`); // some traffic so the charts have a line
    await po.waitForTimeout(6500);
    const tilesText = await po.locator('#tiles').innerText();
    check('ops dashboard shows all replicas ready and live request data',
      /Replicas ready\s*3 \/ 3/.test(tilesText) && (await po.locator('svg path[stroke]').count()) >= 4, tilesText.replace(/\s+/g, ' '));
    check('ops dashboard runs without page errors', pageErrors.length === 0, pageErrors.join(' | '));
    await po.screenshot({ path: join(SHOTS, '6-ops-dashboard.png'), fullPage: true });
  }

  console.log('      4xx responses seen:', JSON.stringify([...new Set(clientErrors)]));
  // The only 4xx we EXPECT: the deliberate wrong code (401 on verify) and CHALLENGE_REQUIRED (403 on enter). The live-status stream
  // (/stream) used to 404 here; the stub now serves it, so any 4xx on it is a real problem. Anything else is a real problem too.
  const unexpected = [...new Set(clientErrors)].filter((e) => !/^401 POST \/api\/auth\/verify$/.test(e) && !/^403 POST \/api\/events\/<id>\/enter$/.test(e));
  check('no unexpected 4xx from the API during the whole run', unexpected.length === 0, unexpected.join(', '));
  check('no 5xx from the API during the whole run', badResponses.length === 0, badResponses.join(', '));
} catch (e) {
  check(`unexpected error: ${String(e).split('\n')[0]}`, false);
  try {
    const pg = browser.contexts()[0]?.pages()[0];
    if (pg) await pg.screenshot({ path: join(SHOTS, 'failure.png') });
  } catch {}
} finally {
  await browser.close();
  await setPreset('none');
}
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} browser checks passed (screenshots: ${SHOTS})`);
process.exit(failed.length ? 1 : 0);
