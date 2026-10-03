// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react';
import { useSessionKeepAlive } from './useSessionKeepAlive';
import { api } from '@/api';
import { ApiError } from '@/api/errors';
import { sessionStore } from '@/state/session';
import { serverClock } from '@/lib/serverClock';

const T0 = Date.parse('2026-11-01T10:00:00.000Z');

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  serverClock.reset();
  window.localStorage.clear();
});

afterEach(() => {
  sessionStore.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('useSessionKeepAlive', () => {
  it('refreshes about five minutes before expiry and stores the new token', async () => {
    vi.spyOn(Math, 'random').mockReturnValue(0);
    const refresh = vi.spyOn(api.auth, 'refresh').mockResolvedValue({
      token: 'new-token',
      expires_at: new Date(T0 + 7_200_000).toISOString(),
      user_id: 'u1',
    });
    sessionStore.set({ token: 'old-token', expires_at: new Date(T0 + 3_600_000).toISOString(), user_id: 'u1' });
    renderHook(() => useSessionKeepAlive());

    await act(async () => void vi.advanceTimersByTime(54 * 60_000));
    expect(refresh).not.toHaveBeenCalled();
    await act(async () => void vi.advanceTimersByTime(2 * 60_000));
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(sessionStore.getSnapshot()?.token).toBe('new-token');
  });

  it('adds jitter so tabs opened together do not refresh together', async () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.99);
    const refresh = vi.spyOn(api.auth, 'refresh').mockResolvedValue({ token: 't', expires_at: new Date(T0 + 7_200_000).toISOString(), user_id: 'u' });
    sessionStore.set({ token: 'x', expires_at: new Date(T0 + 3_600_000).toISOString() });
    renderHook(() => useSessionKeepAlive());
    await act(async () => void vi.advanceTimersByTime(55 * 60_000 + 1000));
    expect(refresh).not.toHaveBeenCalled(); // 0 jitter would have fired by now
    await act(async () => void vi.advanceTimersByTime(30_000));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it('drops the session if the server says it is no longer valid', async () => {
    vi.spyOn(Math, 'random').mockReturnValue(0);
    vi.spyOn(api.auth, 'refresh').mockRejectedValue(new ApiError('UNAUTHENTICATED', 'expired', 401));
    sessionStore.set({ token: 'dead', expires_at: new Date(T0 + 60_000).toISOString() });
    renderHook(() => useSessionKeepAlive());
    await act(async () => void vi.advanceTimersByTime(1000));
    expect(sessionStore.getSnapshot()).toBeNull();
  });

  it('keeps the session through a transient failure', async () => {
    vi.spyOn(Math, 'random').mockReturnValue(0);
    vi.spyOn(api.auth, 'refresh').mockRejectedValue(new ApiError('NETWORK_ERROR', 'down', 0));
    sessionStore.set({ token: 'ok', expires_at: new Date(T0 + 60_000).toISOString() });
    renderHook(() => useSessionKeepAlive());
    await act(async () => void vi.advanceTimersByTime(1000));
    expect(sessionStore.getSnapshot()?.token).toBe('ok');
  });

  it('does nothing when signed out, and cancels on unmount', async () => {
    const refresh = vi.spyOn(api.auth, 'refresh');
    const out = renderHook(() => useSessionKeepAlive());
    await act(async () => void vi.advanceTimersByTime(10 * 3_600_000));
    expect(refresh).not.toHaveBeenCalled();
    out.unmount();

    sessionStore.set({ token: 'x', expires_at: new Date(T0 + 600_000).toISOString() });
    const inn = renderHook(() => useSessionKeepAlive());
    inn.unmount();
    await act(async () => void vi.advanceTimersByTime(3_600_000));
    expect(refresh).not.toHaveBeenCalled();
  });
});
