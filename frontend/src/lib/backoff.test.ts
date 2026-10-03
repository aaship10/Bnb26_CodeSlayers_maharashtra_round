import { backoffDelay, withJitter } from './backoff';

const lo = () => 0;
const hi = () => 0.999999;

describe('backoffDelay', () => {
  it('grows exponentially, staying inside [cap/2, cap]', () => {
    expect(backoffDelay(0, { baseMs: 1000 }, lo)).toBe(500);
    expect(backoffDelay(0, { baseMs: 1000 }, hi)).toBe(1000);
    expect(backoffDelay(3, { baseMs: 1000 }, lo)).toBe(4000);
    expect(backoffDelay(3, { baseMs: 1000 }, hi)).toBe(8000);
  });

  it('never exceeds maxMs', () => {
    expect(backoffDelay(20, { baseMs: 1000, maxMs: 30_000 }, hi)).toBeLessThanOrEqual(30_000);
  });

  it('respects a floor (e.g. the 5 s polling minimum)', () => {
    expect(backoffDelay(0, { baseMs: 1000, floorMs: 5000 }, lo)).toBe(5000);
  });

  it('honours Retry-After as a minimum and adds jitter on top', () => {
    const d = backoffDelay(0, { baseMs: 1000, retryAfterMs: 8000, retryAfterJitterMs: 2000 }, lo);
    expect(d).toBe(8000);
    const d2 = backoffDelay(0, { baseMs: 1000, retryAfterMs: 8000, retryAfterJitterMs: 2000 }, hi);
    expect(d2).toBeGreaterThan(8000);
    expect(d2).toBeLessThanOrEqual(10_000);
  });

  it('spreads real random draws (no thundering herd on one value)', () => {
    const values = new Set(Array.from({ length: 200 }, () => backoffDelay(2)));
    expect(values.size).toBeGreaterThan(50);
  });

  it('treats a negative attempt as the first attempt', () => {
    expect(backoffDelay(-3, { baseMs: 1000 }, lo)).toBe(500);
  });
});

describe('withJitter', () => {
  it('adds between 0 and spreadMs', () => {
    expect(withJitter(1000, 3000, lo)).toBe(1000);
    expect(withJitter(1000, 3000, hi)).toBe(4000);
  });
});
