// @vitest-environment jsdom
import { screen } from '@testing-library/react';
import { TicketPage } from './TicketPage';
import { api } from '@/api';
import type { EventInfo, StatusResponse } from '@/api/schemas';
import { sessionStore } from '@/state/session';
import { renderRoute } from '@/test/render';

const NOW = '2026-11-01T10:31:00.000Z';
const event = { id: 'evt_1', name: 'Moonlight Rooftop Sessions', phase: 'CLAIMING', mode: 'LOTTERY', inventory: 500, window_opens_at: NOW, window_closes_at: NOW, claim_ttl_s: 600, server_now: NOW } as EventInfo;

function setup(status: StatusResponse, state?: unknown) {
  sessionStore.set({ token: 't', user_id: 'u', expires_at: '2099-01-01T00:00:00.000Z' });
  vi.spyOn(api.events, 'get').mockResolvedValue(event);
  vi.spyOn(api.events, 'status').mockResolvedValue(status);
  vi.spyOn(api.auth, 'me').mockResolvedValue({ user_id: 'u', email: 'asha@example.com', display_name: 'Asha' });
  return renderRoute(<TicketPage />, { path: '/events/evt_1/ticket', route: '/events/:id/ticket', state });
}

afterEach(() => {
  vi.restoreAllMocks();
  sessionStore.clear();
});

describe('TicketPage', () => {
  const claimed: StatusResponse = { state: 'CLAIMED', phase: 'CLAIMING', seat_no: 42, ticket_code: 'FD-ABCD-EFGH', server_now: NOW };

  it('shows the event, the holder, the seat and the code (from the server, so it survives a refresh)', async () => {
    setup(claimed);
    expect(await screen.findByTestId('ticket-code')).toHaveTextContent('FD-ABCD-EFGH');
    expect(screen.getByRole('img', { name: 'Seat 42' })).toBeInTheDocument();
    expect(await screen.findByText('Asha')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Moonlight Rooftop Sessions' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Print' })).toBeInTheDocument();
  });

  it('celebrates only right after claiming', async () => {
    const a = setup(claimed, { justClaimed: true });
    expect(await screen.findByRole('heading', { name: /it’s yours/i })).toBeInTheDocument();
    expect(screen.getByTestId('confetti')).toHaveAttribute('aria-hidden', 'true');
    a.unmount();
    vi.restoreAllMocks();

    setup(claimed);
    expect(await screen.findByRole('heading', { name: 'Your ticket' })).toBeInTheDocument();
    expect(screen.queryByTestId('confetti')).not.toBeInTheDocument();
  });

  it('copes with a status that lacks the code (until A adds it): seat still shown, honest note', async () => {
    setup({ ...claimed, ticket_code: undefined });
    expect(await screen.findByText(/your code will appear here shortly/i)).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Seat 42' })).toBeInTheDocument();
  });

  it('no claim, no ticket', async () => {
    setup({ state: 'WON', phase: 'CLAIMING', hold_expires_at: NOW, server_now: NOW });
    expect(await screen.findByText('No ticket here yet')).toBeInTheDocument();
    expect(screen.queryByTestId('ticket-code')).not.toBeInTheDocument();
  });
});
