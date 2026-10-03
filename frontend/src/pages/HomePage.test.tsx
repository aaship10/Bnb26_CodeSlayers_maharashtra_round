// @vitest-environment jsdom
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { HomePage } from './HomePage';
import { api } from '@/api';
import { ApiError } from '@/api/errors';

const event = {
  id: 'evt_1',
  name: 'Moonlight Rooftop Sessions',
  description: 'One night, 500 seats.',
  phase: 'OPEN' as const,
  mode: 'LOTTERY' as const,
  inventory: 500,
  window_opens_at: '2026-11-01T10:00:00.000Z',
  window_closes_at: '2026-11-01T10:30:00.000Z',
  claim_ttl_s: 600,
  seed_commitment: null,
  server_now: '2026-11-01T10:05:00.000Z',
};

function renderHome() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe('HomePage', () => {
  it('lists events from the API with their phase and a link to each', async () => {
    vi.spyOn(api.events, 'list').mockResolvedValue([event]);
    renderHome();
    expect(await screen.findByRole('heading', { name: 'Moonlight Rooftop Sessions' })).toBeInTheDocument();
    expect(screen.getByText('Window open')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View drop' })).toHaveAttribute('href', '/events/evt_1');
    expect(screen.getByText('Seats').nextElementSibling).toHaveTextContent('500');
  });

  it('shows calm, friendly copy (not a raw code) when the server is down, and can retry', async () => {
    const list = vi.spyOn(api.events, 'list').mockRejectedValue(new ApiError('NETWORK_ERROR', 'Network request failed', 0));
    renderHome();
    expect(await screen.findByText("Can't reach the server")).toBeInTheDocument();
    expect(screen.queryByText(/NETWORK_ERROR/)).not.toBeInTheDocument();

    list.mockResolvedValue([event]);
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('heading', { name: 'Moonlight Rooftop Sessions' })).toBeInTheDocument();
  });

  it('has an empty state', async () => {
    vi.spyOn(api.events, 'list').mockResolvedValue([]);
    renderHome();
    expect(await screen.findByText('Nothing on the calendar yet')).toBeInTheDocument();
  });

  it('states the no-rush promise in the hero copy', () => {
    vi.spyOn(api.events, 'list').mockReturnValue(new Promise(() => undefined));
    renderHome();
    expect(screen.getByText(/being first earns you nothing/i)).toBeInTheDocument();
  });
});
