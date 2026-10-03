import { formatCount } from '@/lib/format';
import { utf8 } from '@/features/challenge/sha256';
import { pureHmacFactory, fromHex, toHex, type DrawCrypto } from './cryptoImpl';
import { rankEntrants, split } from './drawEngine';
import { PUBLIC_ID_RE, comparePublicId, entrantsCanonicalText, finalSeedPreimage, resultListText, weightText, type Entrant } from './drawSpec';
import type { Fairness, ResultsResponse } from './schemas';

export type StepId = 'commitment' | 'entrants' | 'seed' | 'rank' | 'results';
export type StepStatus = 'pending' | 'running' | 'pass' | 'fail' | 'skipped';

export interface Step {
  id: StepId;
  title: string;
  status: StepStatus;
  detail?: string;
  ms?: number;
}

export type Verdict = 'verified' | 'failed' | 'not_yet';

export interface VerifyOutcome {
  steps: Step[];
  verdict: Verdict;
  /** Full draw order as recomputed here (for "find yourself"). */
  order?: string[];
  winnersCount?: number;
}

export interface VerifyInput {
  eventId: string;
  fairness: Fairness;
  loadEntrants: () => Promise<Entrant[]>;
  /** Published ordered winners and waitlist, when the API exposes them. */
  loadResults: () => Promise<ResultsResponse | null>;
}

export interface VerifyHooks {
  onSteps?: (steps: Step[]) => void;
  onProgress?: (fraction: number) => void;
}

const TITLES: Record<StepId, string> = {
  commitment: 'The revealed seed matches the commitment published before the window',
  entrants: 'The entrant list is the one locked in when the window closed',
  seed: 'The final seed mixes in the public beacon and the entrant list',
  rank: 'Every entry is ranked by its HMAC',
  results: 'The published winners and waitlist match the recomputed draw',
};

const short = (h: string) => `${h.slice(0, 10)}…${h.slice(-6)}`;

/** Fail-fast validation of a downloaded entrant list. Returns a problem or null. */
export function entrantListProblem(entrants: readonly Entrant[]): string | null {
  for (let i = 0; i < entrants.length; i++) {
    const e = entrants[i]!;
    if (!PUBLIC_ID_RE.test(e.public_id)) return `Entry ${i + 1} has an id we can’t accept: ${JSON.stringify(e.public_id).slice(0, 40)}`;
    try {
      weightText(e.weight);
    } catch {
      return `Entry ${i + 1} (${e.public_id}) has weight ${e.weight}; only 1, 0.5 and 0.25 are allowed`;
    }
    if (i > 0) {
      const c = comparePublicId(entrants[i - 1]!.public_id, e.public_id);
      if (c === 0) return `${e.public_id} appears twice`;
      if (c > 0) return `The list isn’t in canonical order at entry ${i + 1}`;
    }
  }
  return null;
}

/** First index where two ordered lists differ, or -1 if equal. */
export function firstMismatch(a: readonly string[], b: readonly string[]): number {
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++) if (a[i] !== b[i]) return i;
  return -1;
}

/**
 * The five checks, in order, entirely client-side. Each step reports pass/fail
 * with a sentence a non-expert can read. Steps that need data the server hasn't
 * published yet (before the draw) are "skipped", never faked.
 */
export async function verifyDraw(input: VerifyInput, crypto: DrawCrypto, hooks: VerifyHooks = {}): Promise<VerifyOutcome> {
  const { fairness: f } = input;
  const steps: Step[] = (Object.keys(TITLES) as StepId[]).map((id) => ({ id, title: TITLES[id], status: 'pending' }));
  const emit = () => hooks.onSteps?.(steps.map((s) => ({ ...s })));
  const set = (id: StepId, patch: Partial<Step>) => {
    Object.assign(steps.find((s) => s.id === id)!, patch);
    emit();
  };
  const timed = async <T>(id: StepId, fn: () => Promise<T>): Promise<T> => {
    set(id, { status: 'running' });
    const t0 = performance.now();
    try {
      return await fn();
    } finally {
      steps.find((s) => s.id === id)!.ms = Math.round(performance.now() - t0);
    }
  };
  const failAndStop = (id: StepId, detail: string): VerifyOutcome => {
    set(id, { status: 'fail', detail });
    for (const s of steps) if (s.status === 'pending') s.status = 'skipped';
    emit();
    return { steps, verdict: 'failed' };
  };
  const skipRest = (from: StepId, detail: string) => {
    let on = false;
    for (const s of steps) {
      if (s.id === from) on = true;
      if (on && s.status === 'pending') {
        s.status = 'skipped';
        s.detail = detail;
      }
    }
    emit();
  };
  emit();

  /* 1. commitment */
  let serverSeed: Uint8Array | null = null;
  if (!f.server_seed) {
    set('commitment', { status: 'skipped', detail: 'The seed is revealed after the draw. Until then only its fingerprint (the commitment) is public.' });
  } else {
    const ok = await timed('commitment', async () => toHex(await crypto.sha256(fromHex(f.server_seed!))) === f.seed_commitment);
    if (!ok) {
      const got = toHex(await crypto.sha256(fromHex(f.server_seed)));
      return failAndStop('commitment', `SHA-256 of the revealed seed is ${short(got)}, but the commitment published before the window was ${short(f.seed_commitment)}. The seed was changed after the fact.`);
    }
    serverSeed = fromHex(f.server_seed);
    set('commitment', { status: 'pass', detail: `SHA-256(seed) = ${short(f.seed_commitment)}, exactly the published commitment.` });
  }

  /* 2. entrants */
  if (!f.entrants_hash) {
    skipRest('entrants', 'The entrant list is published when the window closes.');
    return { steps, verdict: 'not_yet' };
  }
  let entrants: Entrant[];
  try {
    entrants = await timed('entrants', input.loadEntrants);
  } catch (e) {
    return failAndStop('entrants', `We couldn’t download the entrant list (${e instanceof Error ? e.message : String(e)}).`);
  }
  const problem = entrantListProblem(entrants);
  if (problem) return failAndStop('entrants', `${problem}. The list isn’t in the agreed canonical form.`);
  const entrantsHash = await crypto.sha256(utf8(entrantsCanonicalText(entrants)));
  const eh = toHex(entrantsHash);
  if (eh !== f.entrants_hash) {
    return failAndStop('entrants', `The list we downloaded hashes to ${short(eh)}, but the hash locked in at close is ${short(f.entrants_hash)}. The list has changed since the window closed.`);
  }
  if (f.entrants_count !== null && f.entrants_count !== entrants.length) {
    return failAndStop('entrants', `The list has ${formatCount(entrants.length)} entries but ${formatCount(f.entrants_count)} were announced.`);
  }
  set('entrants', { status: 'pass', detail: `${formatCount(entrants.length)} entries hash to ${short(eh)}, the value locked in at close.` });

  /* 3. final seed */
  if (!serverSeed || !f.beacon?.randomness || !f.final_seed) {
    skipRest('seed', 'The draw hasn’t run yet. These steps unlock once the seed and the beacon are published.');
    return { steps, verdict: 'not_yet' };
  }
  const finalSeed = await timed('seed', async () => crypto.sha256(finalSeedPreimage(serverSeed!, fromHex(f.beacon!.randomness!), input.eventId, entrantsHash)));
  if (toHex(finalSeed) !== f.final_seed) {
    return failAndStop('seed', `Combining the seed, beacon round ${f.beacon.round}, the event id and the entrant hash gives ${short(toHex(finalSeed))}, not the published ${short(f.final_seed)}.`);
  }
  set('seed', { status: 'pass', detail: `Seed + beacon round ${f.beacon.round} + entrant hash gives ${short(f.final_seed)}, as published.` });

  /* 4. rank */
  const ranked = await timed('rank', async () => {
    const sign = await crypto.hmacSigner(finalSeed);
    // Cross-check the native implementation against the pure-JS one on the first entrant.
    if (crypto.name === 'webcrypto' && entrants.length > 0) {
      const a = toHex(await sign(utf8(entrants[0]!.public_id)));
      const b = toHex(pureHmacFactory(finalSeed)(utf8(entrants[0]!.public_id)));
      if (a !== b) throw new Error('WebCrypto and pure-JS HMAC disagree');
    }
    return rankEntrants(entrants, sign, hooks.onProgress);
  });
  const order = ranked.map((r) => r.public_id);
  set('rank', {
    status: 'pass',
    detail: `Ranked ${formatCount(order.length)} entries in ${((steps.find((s) => s.id === 'rank')!.ms ?? 0) / 1000).toFixed(2)} s with ${crypto.name === 'webcrypto' ? 'WebCrypto' : 'pure-JS'} HMAC-SHA256.`,
  });

  /* 5. results */
  const { winners, waitlist } = split(order, f.inventory);
  const outcome = await timed('results', async () => {
    const wh = toHex(await crypto.sha256(utf8(resultListText(winners))));
    const lh = toHex(await crypto.sha256(utf8(resultListText(waitlist))));
    if (!f.result) return { ok: false, detail: 'No result summary has been published to compare against.' };
    if (wh !== f.result.winners_hash) {
      let where = '';
      const pub = await input.loadResults().catch(() => null);
      if (pub) {
        const i = firstMismatch(winners, pub.winners);
        if (i >= 0) where = ` First difference at winner #${i + 1}: the draw gives ${winners[i] ?? '(nobody)'}, the published list has ${pub.winners[i] ?? '(nobody)'}.`;
      }
      return { ok: false, detail: `The recomputed winners hash to ${short(wh)}, the published winners to ${short(f.result.winners_hash)}.${where}` };
    }
    if (lh !== f.result.waitlist_hash) {
      let where = '';
      const pub = await input.loadResults().catch(() => null);
      if (pub) {
        const i = firstMismatch(waitlist, pub.waitlist);
        if (i >= 0) where = ` First difference at waitlist position ${i + 1}: ${waitlist[i] ?? '(nobody)'} vs ${pub.waitlist[i] ?? '(nobody)'}.`;
      }
      return { ok: false, detail: `The recomputed waitlist hashes to ${short(lh)}, the published one to ${short(f.result.waitlist_hash)}.${where}` };
    }
    // Hashes match; if the full lists are published too, compare them entry by entry as well.
    const pub = await input.loadResults().catch(() => null);
    if (pub) {
      const i = firstMismatch(winners, pub.winners);
      if (i >= 0) return { ok: false, detail: `Winner #${i + 1} differs: the draw gives ${winners[i] ?? '(nobody)'}, the published list has ${pub.winners[i] ?? '(nobody)'}.` };
      const j = firstMismatch(waitlist, pub.waitlist);
      if (j >= 0) return { ok: false, detail: `Waitlist position ${j + 1} differs: ${waitlist[j] ?? '(nobody)'} vs ${pub.waitlist[j] ?? '(nobody)'}.` };
    }
    return {
      ok: true,
      detail: `${formatCount(winners.length)} winners and ${formatCount(waitlist.length)} waitlisted, in exactly the published order${pub ? ' (checked entry by entry)' : ' (checked by hash)'}.`,
    };
  });
  if (!outcome.ok) return { ...failAndStop('results', outcome.detail), order, winnersCount: winners.length };
  set('results', { status: 'pass', detail: outcome.detail });
  return { steps, verdict: 'verified', order, winnersCount: winners.length };
}
