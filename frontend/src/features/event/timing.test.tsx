// @vitest-environment jsdom
import { act, render, renderHook, screen } from '@testing-library/react';
import { Countdown, spokenDuration } from './Countdown';
import { useDeadlineRefetch } from './useDeadlineRefetch';
import { serverClock, splitDuration } from '@/lib/serverClock';

const T0 = Date.parse('2026-11-01T10:00:00.000Z');

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  serverClock.reset();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  serverClock.reset();
});

/** Teach the shared clock that the server is `skewMs` ahead of this machine. */
function syncServerAhead(skewMs: number) {
  serverClock.observe(new Date(Date.now() + skewMs).toISOString(), Date.now(), Date.now());
}

describe('Countdown', () => {
  it('counts down against SERVER time, ignoring a wrong local clock', () => {
    syncServerAhead(3_600_000); // this device is an hour behind the server
    const target = new Date(Date.now() + 3_600_000 + 90_000).toISOString(); // server says: 90 s from now
    render(<Countdown target={target} label="Entries open in" />);

    expect(screen.getByTestId('countdown-tiles').textContent).toBe('00hrs01min30sec');

    act(() => void vi.advanceTimersByTime(30_000));
    expect(screen.getByTestId('countdown-tiles').textContent).toBe('00hrs01min00sec');

    act(() => void vi.advanceTimersByTime(1_000));
    expect(screen.getByTestId('countdown-tiles').textContent).toBe('00hrs00min59sec');
  });

  it('shows days only when there are days', () => {
    syncServerAhead(0);
    render(<Countdown target={new Date(T0 + 2 * 86_400_000 + 3_600_000).toISOString()} label="Opens in" />);
    expect(screen.getByTestId('countdown-tiles').textContent).toBe('2days01hrs00min00sec');
  });

  it('hides the ticking digits from assistive tech and gives a minute-resolution sentence', () => {
    syncServerAhead(0);
    render(<Countdown target={new Date(T0 + 125_000).toISOString()} label="Entries open in" />);
    expect(screen.getByTestId('countdown-tiles')).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByText('Entries open in 2 minutes', { exact: false })).toHaveClass('sr-only');
  });

  it('at zero it waits for the server rather than declaring anything open', () => {
    syncServerAhead(0);
    render(<Countdown target={new Date(T0 + 2000).toISOString()} label="Entries open in" />);
    act(() => void vi.advanceTimersByTime(5000));
    expect(screen.getByText('Any moment now…')).toBeInTheDocument();
    expect(screen.queryByTestId('countdown-tiles')).not.toBeInTheDocument();
  });
});

describe('spokenDuration', () => {
  it.each([
    [2 * 86_400_000 + 3 * 3_600_000, '2 days 3 hours'],
    [86_400_000, '1 day'],
    [3 * 3_600_000 + 5 * 60_000, '3 hours 5 minutes'],
    [3_600_000, '1 hour'],
    [5 * 60_000 + 30_000, '5 minutes'], // whole minutes only: never overstate how long is left
    [20_000, 'less than a minute'],
  ])('%d ms -> %s', (ms, expected) => {
    expect(spokenDuration(splitDuration(ms))).toBe(expected);
  });
});

describe('useDeadlineRefetch', () => {
  it('does not fire at the deadline itself: it waits for a random lag first', () => {
    syncServerAhead(0);
    vi.spyOn(Math, 'random').mockReturnValue(0.5); // lag = 300 + 0.5 * 3700 = 2150 ms
    const refetch = vi.fn();
    renderHook(() => useDeadlineRefetch(new Date(T0 + 10_000).toISOString(), refetch));

    act(() => void vi.advanceTimersByTime(10_000));
    expect(refetch).not.toHaveBeenCalled(); // exactly at the deadline: nothing yet
    act(() => void vi.advanceTimersByTime(2_100));
    expect(refetch).not.toHaveBeenCalled();
    act(() => void vi.advanceTimersByTime(100));
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it('spreads clients across at least 3 seconds', () => {
    syncServerAhead(0);
    const fireTimes: number[] = [];
    for (const r of [0, 0.999]) {
      vi.spyOn(Math, 'random').mockReturnValue(r);
      const start = Date.now();
      const refetch = vi.fn(() => fireTimes.push(Date.now() - start));
      const { unmount } = renderHook(() => useDeadlineRefetch(new Date(Date.now() + 1000).toISOString(), refetch));
      act(() => void vi.advanceTimersByTime(6000));
      unmount();
    }
    expect(fireTimes).toHaveLength(2);
    expect(Math.abs(fireTimes[1]! - fireTimes[0]!)).toBeGreaterThanOrEqual(3000);
  });

  it('uses server time: a device whose clock is an hour off fires at the same server moment', () => {
    syncServerAhead(3_600_000);
    vi.spyOn(Math, 'random').mockReturnValue(0);
    const refetch = vi.fn();
    const serverDeadline = new Date(Date.now() + 3_600_000 + 5000).toISOString(); // 5 s away in server terms
    renderHook(() => useDeadlineRefetch(serverDeadline, refetch));
    act(() => void vi.advanceTimersByTime(5200));
    expect(refetch).not.toHaveBeenCalled();
    act(() => void vi.advanceTimersByTime(200));
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it('a deadline that already passed refetches once, with jitter, and then stops', () => {
    syncServerAhead(0);
    vi.spyOn(Math, 'random').mockReturnValue(0);
    const refetch = vi.fn();
    renderHook(() => useDeadlineRefetch(new Date(T0 - 60_000).toISOString(), refetch));
    act(() => void vi.advanceTimersByTime(200));
    expect(refetch).not.toHaveBeenCalled();
    act(() => void vi.advanceTimersByTime(200));
    expect(refetch).toHaveBeenCalledTimes(1);
    act(() => void vi.advanceTimersByTime(120_000));
    expect(refetch).toHaveBeenCalledTimes(1); // no polling loop
  });

  it('reschedules when the deadline changes and cancels on unmount', () => {
    syncServerAhead(0);
    vi.spyOn(Math, 'random').mockReturnValue(0);
    const refetch = vi.fn();
    const { rerender, unmount } = renderHook(({ d }) => useDeadlineRefetch(d, refetch), {
      initialProps: { d: new Date(T0 + 5_000).toISOString() as string | undefined },
    });
    rerender({ d: new Date(T0 + 60_000).toISOString() });
    act(() => void vi.advanceTimersByTime(10_000));
    expect(refetch).not.toHaveBeenCalled(); // the old 5 s timer was cancelled
    unmount();
    act(() => void vi.advanceTimersByTime(120_000));
    expect(refetch).not.toHaveBeenCalled();
  });

  it('does nothing without a deadline', () => {
    const refetch = vi.fn();
    renderHook(() => useDeadlineRefetch(undefined, refetch));
    act(() => void vi.advanceTimersByTime(100_000));
    expect(refetch).not.toHaveBeenCalled();
  });
});
