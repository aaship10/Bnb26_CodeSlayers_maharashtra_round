// @vitest-environment jsdom
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import vectors from './vectors.json';
import { Component as FairnessPage } from './FairnessPage';
import { Component as AuditPage } from './AuditPage';
import type { Fairness } from './schemas';
import { renderRoute } from '@/test/render';

const A = vectors.draw[0]!; // ten-unweighted, 3 seats
const NOW = '2026-11-01T10:40:00.000Z';
const sorted = [...A.entrants].sort((a, b) => (a.public_id < b.public_id ? -1 : 1));

const fairness = (over: Partial<Fairness> = {}): Fairness => ({
  event_id: A.event_id,
  phase: 'CLAIMING',
  algorithm_version: 'fd-draw/1-provisional',
  seed_commitment: A.expected.commitment,
  server_seed: A.server_seed,
  beacon: { source: 'mock-beacon', round: 4242, randomness: A.beacon_randomness },
  entrants_hash: A.expected.entrants_hash,
  entrants_count: 10,
  final_seed: A.expected.final_seed,
  inventory: 3,
  result: { winners_count: 3, waitlist_count: 7, winners_hash: A.expected.winners_hash, waitlist_hash: A.expected.waitlist_hash },
  audit_head_hash: vectors.audit.records[4]!.hash,
  synthetic: true,
  server_now: NOW,
  ...over,
});

const event = {
  id: A.event_id,
  name: 'Vector Night',
  phase: 'CLAIMING',
  mode: 'LOTTERY',
  inventory: 3,
  window_opens_at: '2026-11-01T10:00:00.000Z',
  window_closes_at: '2026-11-01T10:30:00.000Z',
  claim_ttl_s: 600,
  server_now: NOW,
};

/** Route the app's fetches to fixed responses. */
function serve(routes: Record<string, unknown | (() => unknown)>) {
  const seen: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const url = new URL(input, 'http://localhost');
      seen.push(url.pathname + url.search);
      const key = Object.keys(routes).find((k) => (k.includes('?') ? url.pathname + url.search : url.pathname) === k);
      if (!key) return new Response(JSON.stringify({ code: 'NOT_FOUND', message: 'nope' }), { status: 404 });
      const body = routes[key];
      return new Response(JSON.stringify(typeof body === 'function' ? (body as () => unknown)() : body), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }),
  );
  return seen;
}

const base = `/api/events/${A.event_id}`;

afterEach(() => {
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
});

describe('FairnessPage', () => {
  it('shows the public record and badges mock data', async () => {
    serve({ [`${base}/fairness`]: fairness(), [base]: event });
    renderRoute(<FairnessPage />, { path: `/events/${A.event_id}/fairness`, route: '/events/:id/fairness' });
    expect(await screen.findByRole('heading', { name: 'The public record' })).toBeInTheDocument();
    expect(screen.getByText(A.expected.commitment)).toBeInTheDocument();
    expect(screen.getByText(A.server_seed)).toBeInTheDocument();
    expect(screen.getByText('Mock / synthetic data')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /browse and check the audit log/i })).toHaveAttribute('href', `/events/${A.event_id}/audit`);
  });

  it('before the draw: the secret stays secret, and the check says what it can and cannot verify yet', async () => {
    serve({
      [`${base}/fairness`]: fairness({ server_seed: null, final_seed: null, result: null, entrants_hash: null, entrants_count: null, beacon: { source: 'mock-beacon', round: 4242, randomness: null } }),
      [base]: event,
    });
    renderRoute(<FairnessPage />, { path: `/events/${A.event_id}/fairness`, route: '/events/:id/fairness' });
    expect(await screen.findByText(/Kept secret until the draw/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Verify the draw' }));
    expect(await screen.findByText('Not everything can be checked yet')).toBeInTheDocument();
  });

  it('verifies the draw end to end in the browser and finds the person passed from the status page', async () => {
    serve({ [`${base}/fairness`]: fairness(), [base]: event, [`${base}/fairness/entrants`]: { event_id: A.event_id, count: 10, entrants: sorted } });
    const winner2 = A.expected.order[1]!;
    renderRoute(<FairnessPage />, { path: `/events/${A.event_id}/fairness`, route: '/events/:id/fairness', state: { publicId: winner2 } });
    await userEvent.click(await screen.findByRole('button', { name: 'Verify the draw' }));
    expect(await screen.findByText('Verified: this draw is exactly reproducible', {}, { timeout: 5000 })).toBeInTheDocument();
    for (const id of ['commitment', 'entrants', 'seed', 'rank', 'results']) expect(screen.getByTestId(`step-${id}`)).toHaveAttribute('data-status', 'pass');
    expect(screen.getByLabelText('Your draw ID')).toHaveValue(winner2);
    expect(screen.getByText('Picked: winner #2 of 3')).toBeInTheDocument();

    await userEvent.clear(screen.getByLabelText('Your draw ID'));
    await userEvent.type(screen.getByLabelText('Your draw ID'), A.expected.order[5]!);
    expect(screen.getByText('Waitlist position 3')).toBeInTheDocument();

    await userEvent.clear(screen.getByLabelText('Your draw ID'));
    await userEvent.type(screen.getByLabelText('Your draw ID'), 'p_nobody');
    expect(screen.getByText('That draw ID isn’t in the entrant list')).toBeInTheDocument();
  });

  it('a tampered entrant list fails at step 2 with a clear message', async () => {
    const tampered = sorted.map((e, i) => (i === 4 ? { ...e, weight: 0.5 } : e));
    serve({ [`${base}/fairness`]: fairness(), [base]: event, [`${base}/fairness/entrants`]: { event_id: A.event_id, count: 10, entrants: tampered } });
    renderRoute(<FairnessPage />, { path: `/events/${A.event_id}/fairness`, route: '/events/:id/fairness' });
    await userEvent.click(await screen.findByRole('button', { name: 'Verify the draw' }));
    expect(await screen.findByText('This draw does not match its public record', {}, { timeout: 5000 })).toBeInTheDocument();
    const step = screen.getByTestId('step-entrants');
    expect(step).toHaveAttribute('data-status', 'fail');
    expect(step).toHaveTextContent('The list has changed since the window closed');
    expect(screen.getByTestId('step-seed')).toHaveAttribute('data-status', 'skipped');
  });
});

describe('AuditPage', () => {
  const recs = vectors.audit.records;
  const page = (records: unknown[]) => ({ event_id: A.event_id, records, next_from_seq: null, head: { seq: 5, hash: recs[4]!.hash } });
  const serverSaysOk = { ok: true, head_seq: 5, head_hash: recs[4]!.hash, checked: 5, first_bad_seq: null };

  it('lists records, highlights the head, and confirms an intact chain against the server and the fairness page', async () => {
    serve({ [`${base}/audit?from_seq=1&limit=50`]: page(recs), [`${base}/audit/verify`]: serverSaysOk, [`${base}/fairness`]: fairness() });
    renderRoute(<AuditPage />, { path: `/events/${A.event_id}/audit`, route: '/events/:id/audit' });
    expect(await screen.findByText('EVENT_CREATED')).toBeInTheDocument();
    expect(within(screen.getByTestId('audit-5')).getByText('Head')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/same as on the fairness page/)).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: 'Check the chain in my browser' }));
    expect(await screen.findByText('Chain intact')).toBeInTheDocument();
    for (let s = 1; s <= 5; s++) expect(screen.getByTestId(`audit-${s}`)).toHaveAttribute('data-check', 'ok');
  });

  it('catches an edited record even when the server claims everything is fine', async () => {
    const edited = recs.map((r) => (r.seq === 3 ? { ...r, payload: { ...r.payload, note: 'edited later' } } : r));
    serve({ [`${base}/audit?from_seq=1&limit=50`]: page(edited), [`${base}/audit/verify`]: serverSaysOk, [`${base}/fairness`]: fairness() });
    renderRoute(<AuditPage />, { path: `/events/${A.event_id}/audit`, route: '/events/:id/audit' });
    await userEvent.click(await screen.findByRole('button', { name: 'Check the chain in my browser' }));
    expect(await screen.findByText('Record #3 breaks the chain')).toBeInTheDocument();
    expect(screen.getByText(/don’t take its word for it/)).toBeInTheDocument();
    expect(screen.getByTestId('audit-3')).toHaveAttribute('data-check', 'bad');
    expect(screen.getByTestId('audit-4')).toHaveAttribute('data-check', 'ok');
  });
});
