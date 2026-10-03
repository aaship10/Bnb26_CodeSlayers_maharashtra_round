// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { Component as AdminEventPage } from './AdminEventPage';
import { Component as AdminHome } from './AdminHome';
import { adminApi } from './adminApi';
import { adminToken } from './adminToken';
import type { AdminEvent, DefenceConfig, Invariants, Preset, Stats } from './schemas';
import { ApiError } from '@/api/errors';
import { Dialog } from '@/ui/Dialog';
import { renderRoute } from '@/test/render';

const NOW = '2026-11-01T10:10:00.000Z';
const layers = (on: string[]): DefenceConfig['layers'] => ({
  rate_limit: { enabled: on.includes('rate_limit'), per_ip_rps: 5 },
  pow: { enabled: on.includes('pow'), difficulty_bits: 18 },
  captcha: { enabled: on.includes('captcha') },
  signals: { enabled: on.includes('signals') },
  risk: { enabled: on.includes('risk') },
});
const PRESETS: Preset[] = [
  { id: 'none', name: 'None', description: 'nothing', defences: { preset: 'none', layers: layers([]) } },
  { id: 'rate_limit', name: 'Rate limit', description: 'rl', defences: { preset: 'rate_limit', layers: layers(['rate_limit']) } },
  { id: 'rate_limit+pow', name: 'Rate limit + proof-of-work', description: 'rl pow', defences: { preset: 'rate_limit+pow', layers: layers(['rate_limit', 'pow']) } },
];
const event = (over: Partial<AdminEvent> = {}): AdminEvent => ({
  id: 'evt_1',
  name: 'Moonlight Rooftop Sessions',
  phase: 'SCHEDULED',
  mode: 'LOTTERY',
  inventory: 500,
  window_opens_at: '2026-11-01T10:00:00.000Z',
  window_closes_at: '2026-11-01T10:30:00.000Z',
  claim_ttl_s: 600,
  server_now: NOW,
  config: { defences: { preset: 'rate_limit', layers: layers(['rate_limit']) } },
  ...over,
});
const stats = (over: Partial<Stats> = {}): Stats => ({
  event_id: 'evt_1',
  phase: 'CLAIMING',
  by_state: { REGISTERED: 2300, ENTERED: 0, WON: 120, WAITLISTED: 49_500, CLAIMED: 380, EXPIRED: 0, LOST: 0 },
  entrants: 50_000,
  allocations: { inventory: 500, claimed: 380, held: 120, available: 0 },
  holds: { active: 120, expired: 0 },
  server_now: NOW,
  ...over,
});
const okInv: Invariants = { oversold: 0, duplicate_users: 0, duplicate_seats: 0, orphaned_holds: 0, passed: true, checked_at: NOW };

function setupEventPage(ev = event(), opts: { stats?: Stats; invariants?: Invariants } = {}) {
  adminToken.set('tok');
  vi.spyOn(adminApi, 'event').mockResolvedValue(ev);
  vi.spyOn(adminApi, 'presets').mockResolvedValue(PRESETS);
  vi.spyOn(adminApi, 'stats').mockResolvedValue(opts.stats ?? stats());
  vi.spyOn(adminApi, 'invariants').mockResolvedValue(opts.invariants ?? okInv);
  return renderRoute(<AdminEventPage />, { path: '/admin/events/evt_1', route: '/admin/events/:id' });
}

beforeEach(() => window.sessionStorage.clear());
afterEach(() => {
  vi.restoreAllMocks();
  adminToken.clear();
});

describe('Dialog', () => {
  function Harness() {
    const [open, setOpen] = useState(false);
    return (
      <>
        <button onClick={() => setOpen(true)}>open it</button>
        <Dialog open={open} title="Hello" onClose={() => setOpen(false)}>
          <button>first</button>
          <button>last</button>
        </Dialog>
      </>
    );
  }

  it('moves focus in, traps Tab, closes on Esc and returns focus', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const opener = screen.getByRole('button', { name: 'open it' });
    await user.click(opener);
    const dialog = screen.getByRole('dialog', { name: 'Hello' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(within(dialog).getByRole('button', { name: 'Close' })).toHaveFocus();
    await user.tab();
    await user.tab();
    expect(within(dialog).getByRole('button', { name: 'last' })).toHaveFocus();
    await user.tab(); // wraps
    expect(within(dialog).getByRole('button', { name: 'Close' })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });
});

describe('unlock gate', () => {
  it('asks for the token in a modal and loads nothing until it is verified', async () => {
    const events = vi.spyOn(adminApi, 'events').mockResolvedValue([event()]);
    const verify = vi.spyOn(adminApi, 'verifyToken');
    renderRoute(<AdminHome />, { path: '/admin', route: '/admin' });
    expect(screen.getByRole('dialog', { name: 'Organizer access' })).toBeInTheDocument();
    expect(events).not.toHaveBeenCalled();

    verify.mockRejectedValueOnce(new ApiError('FORBIDDEN', 'no', 403));
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Admin token'), 'wrong');
    await user.click(screen.getByRole('button', { name: 'Unlock' }));
    expect(await screen.findByText(/wasn’t accepted/)).toBeInTheDocument();
    expect(adminToken.get()).toBeNull();

    verify.mockResolvedValueOnce(PRESETS);
    await user.clear(screen.getByLabelText('Admin token'));
    await user.type(screen.getByLabelText('Admin token'), 'dev-admin-token');
    await user.click(screen.getByRole('button', { name: 'Unlock' }));
    expect(await screen.findByRole('heading', { name: 'Events' })).toBeInTheDocument();
    expect(window.sessionStorage.getItem('fd.admin_token')).toBe('dev-admin-token');
    expect(window.localStorage.getItem('fd.admin_token')).toBeNull();
    expect(screen.getByTestId('location').textContent).not.toContain('dev-admin-token');
    expect(await screen.findByText('Moonlight Rooftop Sessions')).toBeInTheDocument();
  });

  it('Lock forgets the token', async () => {
    adminToken.set('tok');
    vi.spyOn(adminApi, 'events').mockResolvedValue([]);
    renderRoute(<AdminHome />, { path: '/admin', route: '/admin' });
    await userEvent.click(await screen.findByRole('button', { name: 'Lock' }));
    expect(adminToken.get()).toBeNull();
    expect(screen.getByRole('dialog', { name: 'Organizer access' })).toBeInTheDocument();
  });
});

describe('event control page', () => {
  it('only the valid next step is enabled', async () => {
    setupEventPage(event({ phase: 'SCHEDULED' }));
    expect(await screen.findByRole('button', { name: 'Open window' })).toBeEnabled();
    for (const name of ['Schedule', 'Close window', 'Run draw']) expect(screen.getByRole('button', { name })).toBeDisabled();
    expect(screen.getByRole('listitem', { current: 'step' })).toHaveTextContent('Scheduled');
  });

  it('asks before acting, sends an Idempotency-Key, and shows server refusals inside the dialog', async () => {
    const transition = vi.spyOn(adminApi, 'transition');
    setupEventPage(event({ phase: 'OPEN' }));
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Close window' }));
    const dialog = screen.getByRole('dialog', { name: 'Close the entry window?' });
    expect(transition).not.toHaveBeenCalled(); // nothing on the first click

    transition.mockRejectedValueOnce(new ApiError('VALIDATION_ERROR', 'Can’t close an event that is DRAWING', 409));
    await user.click(within(dialog).getByRole('button', { name: 'Close window' }));
    expect(await within(dialog).findByText('Can’t close an event that is DRAWING')).toBeInTheDocument();
    const key1 = transition.mock.calls[0]![2];
    expect(key1).toMatch(/^[0-9a-f-]{36}$/);

    transition.mockResolvedValueOnce(event({ phase: 'DRAWING' }));
    await user.click(within(dialog).getByRole('button', { name: 'Close window' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(transition.mock.calls[1]![2]).toBe(key1); // a retry of the same confirmation reuses its key
    expect(await screen.findByRole('button', { name: 'Run draw' })).toBeEnabled();
  });

  it('the draw confirmation states what will happen, with real numbers', async () => {
    setupEventPage(event({ phase: 'DRAWING' }), { stats: stats({ entrants: 48_210 }) });
    await screen.findByText('48.2K');
    await userEvent.click(screen.getByRole('button', { name: 'Run draw' }));
    expect(screen.getByRole('dialog', { name: 'Run the draw?' })).toHaveTextContent('Picks 500 winners from 48,210 entries');
  });

  it('invariants: green with numbers when clean, a loud banner when violated', async () => {
    const a = setupEventPage();
    const badge = await screen.findByTestId('invariants');
    await waitFor(() => expect(badge).toHaveAttribute('data-state', 'ok'));
    expect(badge).toHaveTextContent('oversold 0 · duplicates 0 · orphaned holds 0');
    a.unmount();
    vi.restoreAllMocks();

    setupEventPage(event(), { invariants: { ...okInv, oversold: 1, duplicate_seats: 2, passed: false } });
    await waitFor(() => expect(screen.getByTestId('invariants')).toHaveAttribute('data-state', 'bad'));
    expect(screen.getByText('An allocation invariant is violated')).toBeInTheDocument();
    expect(screen.getByTestId('invariants')).toHaveTextContent('oversold 1 · duplicates 2');
  });

  it('a server that says passed but reports a non-zero counter is still treated as violated', async () => {
    setupEventPage(event(), { invariants: { ...okInv, orphaned_holds: 3, passed: true } });
    await waitFor(() => expect(screen.getByTestId('invariants')).toHaveAttribute('data-state', 'bad'));
  });

  it('stats: tiles, allocation with numbers in the legend, and a MOCK badge on synthetic data', async () => {
    const a = setupEventPage(event(), { stats: stats({ synthetic: true }) });
    expect(await screen.findByText('50K')).toBeInTheDocument();
    expect(screen.getByText('Mock / synthetic data')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /380 claimed, 120 held, 0 not yet allocated, of 500 seats/ })).toBeInTheDocument();
    expect(screen.getByRole('rowheader', { name: 'Waitlisted' }).closest('tr')).toHaveTextContent('49,500');
    a.unmount();
    vi.restoreAllMocks();

    setupEventPage(event(), { stats: stats({ synthetic: false }) });
    await screen.findByText('50K');
    expect(screen.queryByText('Mock / synthetic data')).not.toBeInTheDocument();
  });

  it('defences: flipping a layer makes the preset custom and dirty; Apply PATCHes the full config', async () => {
    const patch = vi.spyOn(adminApi, 'patchConfig').mockImplementation(async (_id, defences) => event({ config: { defences } }));
    setupEventPage();
    const user = userEvent.setup();
    await screen.findByRole('radio', { name: 'Rate limit' });
    expect(screen.getByRole('radio', { name: 'Rate limit' })).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByText('Matches what’s live')).toBeInTheDocument();

    await user.click(screen.getByRole('switch', { name: 'CAPTCHA' }));
    expect(screen.getByRole('radio', { name: 'Custom' })).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument();

    await user.click(screen.getByRole('switch', { name: 'CAPTCHA' }));
    await user.click(screen.getByRole('switch', { name: 'Proof-of-work' }));
    expect(screen.getByRole('radio', { name: 'Rate limit + proof-of-work' })).toHaveAttribute('aria-checked', 'true');

    await user.click(screen.getByRole('button', { name: 'Apply' }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const [, sent, key] = patch.mock.calls[0]!;
    expect(sent.preset).toBe('rate_limit+pow');
    expect(sent.layers.pow).toEqual({ enabled: true, difficulty_bits: 18 }); // parameters preserved
    expect(key).toMatch(/^[0-9a-f-]{36}$/);
    expect(await screen.findByText('Defences updated')).toBeInTheDocument();
  });

  it('the raw JSON editor rejects invalid config with readable reasons and accepts a valid one', async () => {
    setupEventPage();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Edit as JSON' }));
    const box = screen.getByLabelText('Raw config (JSON)');
    await user.clear(box);
    await user.click(box);
    await user.paste('{"preset":"all","layers":{"rate_limit":{"enabled":"yes"}}}');
    await user.click(screen.getByRole('button', { name: 'Use this JSON' }));
    const problems = await screen.findByText('That config isn’t valid');
    expect(problems.closest('[role]')).toHaveTextContent('layers.rate_limit.enabled');
    expect(problems.closest('[role]')).toHaveTextContent('layers.pow');

    await user.clear(box);
    await user.paste(JSON.stringify(PRESETS[2]!.defences));
    await user.click(screen.getByRole('button', { name: 'Use this JSON' }));
    expect(screen.getByRole('radio', { name: 'Rate limit + proof-of-work' })).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument();
  });

  it('reset (dev only) needs the word RESET typed before it will run', async () => {
    const reset = vi.spyOn(adminApi, 'reset').mockResolvedValue(event({ phase: 'DRAFT' }));
    setupEventPage();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Reset this event…' }));
    const dialog = screen.getByRole('dialog');
    const go = within(dialog).getByRole('button', { name: 'Reset everything' });
    expect(go).toBeDisabled();
    await user.type(within(dialog).getByLabelText('Type RESET to confirm'), 'reset');
    expect(go).toBeDisabled(); // exact text only
    await user.clear(within(dialog).getByLabelText('Type RESET to confirm'));
    await user.type(within(dialog).getByLabelText('Type RESET to confirm'), 'RESET');
    await user.click(go);
    await waitFor(() => expect(reset).toHaveBeenCalledTimes(1));
    expect(reset.mock.calls[0]![1]).toMatch(/^[0-9a-f-]{36}$/);
  });
});

describe('create event', () => {
  it('validates, converts local times to UTC, sends an Idempotency-Key and opens the new event', async () => {
    adminToken.set('tok');
    vi.spyOn(adminApi, 'events').mockResolvedValue([]);
    vi.spyOn(adminApi, 'presets').mockResolvedValue(PRESETS);
    const create = vi.spyOn(adminApi, 'create').mockResolvedValue(event({ id: 'evt_new', phase: 'DRAFT', name: 'Test drop' }));
    renderRoute(<AdminHome />, { path: '/admin', route: '/admin' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'New event' }));
    const dialog = screen.getByRole('dialog', { name: 'New event' });

    await user.click(within(dialog).getByRole('button', { name: 'Create as draft' }));
    expect(within(dialog).getByText('Give the event a name.')).toBeInTheDocument();
    expect(create).not.toHaveBeenCalled();

    await user.type(within(dialog).getByLabelText('Name'), 'Test drop');
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Create as draft' })).toBeEnabled());
    await user.click(within(dialog).getByRole('button', { name: 'Create as draft' }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    const [body, key] = create.mock.calls[0]!;
    expect(body).toMatchObject({ name: 'Test drop', inventory: 500, claim_ttl_s: 600, mode: 'LOTTERY' });
    expect(body.window_opens_at).toMatch(/Z$/);
    expect(Date.parse(body.window_closes_at) - Date.parse(body.window_opens_at)).toBe(30 * 60_000);
    expect(body.config.defences.preset).toBe('rate_limit+pow');
    expect(key).toMatch(/^[0-9a-f-]{36}$/);
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/admin/events/evt_new'));
  });
});
