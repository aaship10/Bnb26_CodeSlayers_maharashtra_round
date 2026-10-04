// @vitest-environment jsdom
import { act, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { StatusCard } from './StatusCard';
import { HoldTimer } from './HoldTimer';
import { deriveStatusView, VIEW_ANNOUNCEMENT } from './statusView';
import { AnnouncerProvider } from '@/app/Announcer';
import type { EventInfo, Phase, StatusResponse, UserState } from '@/api/schemas';
import { PHASES, USER_STATES } from '@/api/schemas';
import { serverClock } from '@/lib/serverClock';

const NOW = Date.parse('2026-11-01T10:31:00.000Z');
const iso = (ms: number) => new Date(ms).toISOString();

const event: EventInfo = {
  id: 'evt_1',
  name: 'Moonlight Rooftop Sessions',
  phase: 'OPEN',
  mode: 'LOTTERY',
  inventory: 500,
  window_opens_at: '2026-11-01T10:00:00.000Z',
  window_closes_at: '2026-11-01T10:30:00.000Z',
  claim_ttl_s: 600,
  server_now: iso(NOW),
};

function show(status: Partial<StatusResponse> & { state: UserState; phase: Phase }) {
  return render(
    <AnnouncerProvider>
      <MemoryRouter>
        <StatusCard status={{ server_now: iso(NOW), ...status }} event={event} />
      </MemoryRouter>
    </AnnouncerProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: false });
  vi.setSystemTime(NOW);
  serverClock.reset();
  serverClock.observe(iso(NOW), NOW, NOW);
});
afterEach(() => {
  vi.useRealTimers();
  serverClock.reset();
});

describe('deriveStatusView', () => {
  it('covers every state x phase combination without throwing', () => {
    for (const state of USER_STATES) for (const phase of PHASES) expect(VIEW_ANNOUNCEMENT[deriveStatusView({ state, phase })]).toBeTruthy();
  });

  it.each([
    ['REGISTERED', 'SCHEDULED', 'not_open'],
    ['REGISTERED', 'DRAFT', 'not_open'],
    ['REGISTERED', 'OPEN', 'not_entered'],
    ['REGISTERED', 'DRAWING', 'missed'],
    ['REGISTERED', 'CLOSED', 'missed'],
    ['ENTERED', 'OPEN', 'waiting'],
    ['ENTERED', 'DRAWING', 'drawing'],
    ['ENTERED', 'CLAIMING', 'drawing'], // result not landed yet: still "drawing", never a guess
    ['WON', 'CLAIMING', 'won'],
    ['WAITLISTED', 'CLAIMING', 'waitlisted'],
    ['LOST', 'CLOSED', 'lost'],
    ['CLAIMED', 'CLAIMING', 'claimed'],
    ['EXPIRED', 'CLOSED', 'expired'],
  ] as const)('%s during %s -> %s', (state, phase, view) => {
    expect(deriveStatusView({ state, phase })).toBe(view);
  });
});

describe('StatusCard: every state has the right copy and controls', () => {
  it('not open yet: countdown to the opening, no claim, link to the drop', () => {
    show({ state: 'REGISTERED', phase: 'SCHEDULED' });
    expect(screen.getByRole('heading', { name: 'The window hasn’t opened yet' })).toBeInTheDocument();
    expect(screen.getByText('Entries open in')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Go to the drop' })).toHaveAttribute('href', '/events/evt_1');
    expect(screen.queryByRole('link', { name: /claim/i })).not.toBeInTheDocument();
  });

  it('not entered: sends them to enter', () => {
    show({ state: 'REGISTERED', phase: 'OPEN' });
    expect(screen.getByRole('heading', { name: 'You haven’t entered yet' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Enter now' })).toHaveAttribute('href', '/events/evt_1');
  });

  it('entered and waiting: reassures, shows NO rank or position, nothing to click', () => {
    const { container } = show({ state: 'ENTERED', phase: 'OPEN' });
    expect(screen.getByRole('heading', { name: 'You’re in the draw' })).toBeInTheDocument();
    expect(screen.getByText(/nobody has a position/)).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/rank|#\d|position \d|number \d|odds/i);
    expect(screen.queryAllByRole('link')).toHaveLength(0);
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });

  it('drawing: says it is running and that the page updates itself', () => {
    show({ state: 'ENTERED', phase: 'DRAWING' });
    expect(screen.getByRole('heading', { name: 'The draw is running' })).toBeInTheDocument();
    expect(screen.getByText(/no need to refresh/)).toBeInTheDocument();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });

  it('won: live hold timer and a claim action', () => {
    show({ state: 'WON', phase: 'CLAIMING', hold_expires_at: iso(NOW + 9 * 60_000 + 30_000), public_id: 'p_abc123' });
    expect(screen.getByRole('heading', { name: 'You’ve been picked!' })).toBeInTheDocument();
    expect(screen.getByTestId('hold-timer')).toHaveTextContent('09:30');
    expect(screen.getAllByRole('link', { name: 'Claim my seat' })).toHaveLength(1); // one clear action, not two
    expect(screen.getByRole('link', { name: 'Claim my seat' })).toHaveAttribute('href', '/events/evt_1/claim');
    expect(screen.getByText('p_abc123')).toBeInTheDocument();
  });

  it('waitlisted with a position from the server shows it', () => {
    show({ state: 'WAITLISTED', phase: 'CLAIMING', waitlist_position: 137 });
    expect(screen.getByRole('heading', { name: 'You’re on the waitlist' })).toBeInTheDocument();
    expect(screen.getByText(/You’re number 137 in line/)).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Waitlist position 137' })).toBeInTheDocument();
  });

  it('waitlisted WITHOUT a position never invents one', () => {
    const { container } = show({ state: 'WAITLISTED', phase: 'CLAIMING' });
    expect(screen.getByRole('heading', { name: 'You’re on the waitlist' })).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/number \d|position \d/i);
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });

  it('lost: kind, points to the fairness evidence, no actions to take', () => {
    show({ state: 'LOST', phase: 'CLOSED', public_id: 'p_x' });
    expect(screen.getByRole('heading', { name: 'Not this time' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'See how the draw was made' })).toHaveAttribute('href', '/events/evt_1/fairness');
    expect(screen.queryByRole('link', { name: /claim/i })).not.toBeInTheDocument();
  });

  it('claimed: seat and a link to the ticket', () => {
    show({ state: 'CLAIMED', phase: 'CLAIMING', seat_no: 42, ticket_code: 'FD-AAAA-BBBB' });
    expect(screen.getByRole('heading', { name: 'Your seat is confirmed' })).toBeInTheDocument();
    expect(screen.getByText(/Seat 42 is yours/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View ticket' })).toHaveAttribute('href', '/events/evt_1/ticket');
  });

  it('hold expired: calm, no claim action', () => {
    show({ state: 'EXPIRED', phase: 'CLOSED' });
    expect(screen.getByRole('heading', { name: 'Your hold ran out' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /claim/i })).not.toBeInTheDocument();
  });

  it('window closed without an entry', () => {
    show({ state: 'REGISTERED', phase: 'CLAIMING' });
    expect(screen.getByRole('heading', { name: 'You didn’t enter this drop' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /enter|claim/i })).not.toBeInTheDocument();
  });

  it('copy stays calm: no shouting or alarm words in any state', () => {
    const cases: [UserState, Phase][] = [
      ['REGISTERED', 'SCHEDULED'],
      ['REGISTERED', 'OPEN'],
      ['ENTERED', 'OPEN'],
      ['ENTERED', 'DRAWING'],
      ['WAITLISTED', 'CLAIMING'],
      ['LOST', 'CLOSED'],
      ['EXPIRED', 'CLOSED'],
      ['REGISTERED', 'CLOSED'],
    ];
    for (const [state, phase] of cases) {
      const { container, unmount } = show({ state, phase });
      expect(container.textContent, `${state}/${phase}`).not.toMatch(/!|error|failed|warning|urgent|hurry|denied/i);
      unmount();
    }
  });
});

describe('HoldTimer', () => {
  function timer(expiresMs: number, onElapsed = vi.fn()) {
    render(
      <AnnouncerProvider>
        <HoldTimer expiresAt={iso(expiresMs)} onElapsed={onElapsed} />
      </AnnouncerProvider>,
    );
    return onElapsed;
  }

  it('counts against server time even when the device clock is wrong', () => {
    // The device is 2 hours behind; the server says the hold ends 5 minutes from ITS now.
    vi.setSystemTime(NOW - 2 * 3_600_000);
    serverClock.reset();
    serverClock.observe(iso(NOW), Date.now(), Date.now());
    timer(NOW + 5 * 60_000);
    expect(screen.getByTestId('hold-timer')).toHaveTextContent('05:00');
  });

  it('turns urgent under a minute and says so once', () => {
    timer(NOW + 61_000);
    expect(screen.getByTestId('hold-timer')).toHaveAttribute('data-urgent', 'false');
    act(() => void vi.advanceTimersByTime(2_000));
    expect(screen.getByTestId('hold-timer')).toHaveAttribute('data-urgent', 'true');
    expect(screen.getByTestId('announcer')).toHaveTextContent('Less than a minute left');
  });

  it('at zero it hands over to the server (onElapsed once) instead of declaring anything', () => {
    const onElapsed = timer(NOW + 3_000);
    act(() => void vi.advanceTimersByTime(5_000));
    expect(screen.getByText('Time’s up. Checking with the server…')).toBeInTheDocument();
    act(() => void vi.advanceTimersByTime(10_000));
    expect(onElapsed).toHaveBeenCalledTimes(1);
  });
});
