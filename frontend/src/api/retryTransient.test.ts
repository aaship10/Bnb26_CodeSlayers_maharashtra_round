import { ApiError } from './errors';
import { abortableSleep, isTransient, retryTransient } from './retryTransient';

const net = () => new ApiError('NETWORK_ERROR', 'down', 0);
const noSleep = vi.fn().mockResolvedValue(undefined);

beforeEach(() => noSleep.mockClear());

describe('isTransient', () => {
  it('network, timeout and 5xx yes', () => {
    expect(isTransient(net())).toBe(true);
    expect(isTransient(new ApiError('TIMEOUT', 'x', 0))).toBe(true);
    expect(isTransient(new ApiError('INTERNAL', 'x', 503))).toBe(true);
  });
  it('4xx, 429, challenges and non-api errors no', () => {
    expect(isTransient(new ApiError('RATE_LIMITED', 'x', 429))).toBe(false);
    expect(isTransient(new ApiError('REJECTED', 'x', 403))).toBe(false);
    expect(isTransient(new ApiError('CHALLENGE_REQUIRED', 'x', 403))).toBe(false);
    expect(isTransient(new ApiError('WINDOW_CLOSED', 'x', 409))).toBe(false);
    expect(isTransient(new Error('x'))).toBe(false);
  });
});

describe('retryTransient', () => {
  it('returns the first success without sleeping', async () => {
    const fn = vi.fn().mockResolvedValue(1);
    await expect(retryTransient(fn, { sleep: noSleep })).resolves.toBe(1);
    expect(noSleep).not.toHaveBeenCalled();
  });

  it('retries transient failures, reporting each retry, then succeeds', async () => {
    const fn = vi.fn().mockRejectedValueOnce(net()).mockRejectedValueOnce(net()).mockResolvedValueOnce('ok');
    const onRetry = vi.fn();
    await expect(retryTransient(fn, { retries: 2, sleep: noSleep, onRetry })).resolves.toBe('ok');
    expect(fn).toHaveBeenCalledTimes(3);
    expect(onRetry.mock.calls.map((c) => c[0])).toEqual([1, 2]);
  });

  it('backs off with at least the floor each time (no immediate hammering)', async () => {
    const fn = vi.fn().mockRejectedValueOnce(net()).mockRejectedValueOnce(net()).mockResolvedValueOnce('ok');
    await retryTransient(fn, { retries: 2, sleep: noSleep });
    for (const call of noSleep.mock.calls) expect(call[0]).toBeGreaterThanOrEqual(800);
  });

  it('gives up after the retry budget and throws the last error', async () => {
    const e = net();
    const fn = vi.fn().mockRejectedValue(e);
    await expect(retryTransient(fn, { retries: 2, sleep: noSleep })).rejects.toBe(e);
    expect(fn).toHaveBeenCalledTimes(3);
  });

  it('does not retry non-transient errors', async () => {
    const e = new ApiError('REJECTED', 'no', 403);
    const fn = vi.fn().mockRejectedValue(e);
    await expect(retryTransient(fn, { sleep: noSleep })).rejects.toBe(e);
    expect(fn).toHaveBeenCalledTimes(1);
  });
});

describe('abortableSleep', () => {
  it('resolves after the delay', async () => {
    const t0 = Date.now();
    await abortableSleep(30);
    expect(Date.now() - t0).toBeGreaterThanOrEqual(25);
  });

  it('rejects promptly when aborted', async () => {
    const c = new AbortController();
    const p = abortableSleep(10_000, c.signal);
    c.abort();
    await expect(p).rejects.toMatchObject({ name: 'AbortError' });
  });
});
