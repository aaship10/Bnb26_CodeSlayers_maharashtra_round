import { ServerClock, splitDuration } from './serverClock';

/** Build a clock whose local time we control. */
function clockAt(localMs: { v: number }) {
  return new ServerClock(() => localMs.v);
}

describe('ServerClock', () => {
  it('is a no-op (offset 0) until it has seen a response', () => {
    const local = { v: 1_000_000 };
    const c = clockAt(local);
    expect(c.synced).toBe(false);
    expect(c.offsetMs).toBe(0);
    expect(c.now()).toBe(1_000_000);
  });

  it('computes offset from server_now assuming a symmetric round trip', () => {
    const local = { v: 0 };
    const c = clockAt(local);
    // Local clock is 5 s *behind* the server. Request sent at local t=1000, answered at t=1200 (rtt 200).
    // Server stamped its time mid-flight: local 1100 + 5000 skew.
    c.observe(new Date(6100).toISOString(), 1000, 1200);
    expect(c.offsetMs).toBe(5000);
    local.v = 2000;
    expect(c.now()).toBe(7000);
  });

  it('handles a local clock that is AHEAD of the server', () => {
    const c = clockAt({ v: 0 });
    c.observe(new Date(10_000).toISOString(), 60_000, 60_100); // server says 10_000 at local ~60_050
    expect(c.offsetMs).toBe(10_000 - 60_050);
  });

  it('trusts the lowest-RTT sample', () => {
    const c = clockAt({ v: 0 });
    // noisy sample: rtt 2000, implies offset 5000 + (skewed by asymmetric delay)
    c.observe(new Date(6000 + 1000).toISOString(), 1000, 3000); // offset = 7000 - 2000 = 5000
    // precise sample: rtt 20, true offset 4000
    c.observe(new Date(5010 + 4000).toISOString(), 5000, 5020); // offset = 9010 - 5010 = 4000
    expect(c.offsetMs).toBe(4000);
  });

  it('forgets samples older than two minutes so drift can correct itself', () => {
    const c = clockAt({ v: 0 });
    c.observe(new Date(1000 + 9000).toISOString(), 1000, 1002); // great rtt, offset ~ 9000
    // three minutes later a worse sample arrives with a different offset
    c.observe(new Date(181_000 + 20 + 7000).toISOString(), 181_000, 181_040); // offset 7000
    expect(c.offsetMs).toBe(7000);
  });

  it('ignores garbage timestamps', () => {
    const c = clockAt({ v: 0 });
    c.observe('not a date', 0, 10);
    expect(c.synced).toBe(false);
  });

  it('msUntil counts down against server time and goes negative once passed', () => {
    const local = { v: 0 };
    const c = clockAt(local);
    c.observe(new Date(1000).toISOString(), 1000, 1000); // server 1000 when local said 1000: offset 0
    local.v = 1000;
    expect(c.msUntil(new Date(4000).toISOString())).toBe(3000);
    local.v = 5000;
    expect(c.msUntil(new Date(4000).toISOString())).toBe(-1000);
  });

  it('a shifted local clock does not change what the countdown shows', () => {
    // Same server deadline, two devices whose clocks disagree by an hour: both count down identically.
    const deadline = new Date(1_000_000 + 30_000).toISOString();
    const a = clockAt({ v: 1_000_000 });
    a.observe(new Date(1_000_000).toISOString(), 1_000_000, 1_000_000);
    const b = clockAt({ v: 1_000_000 + 3_600_000 });
    b.observe(new Date(1_000_000).toISOString(), 1_000_000 + 3_600_000, 1_000_000 + 3_600_000);
    expect(a.msUntil(deadline)).toBe(30_000);
    expect(b.msUntil(deadline)).toBe(30_000);
  });

  it('reset forgets everything', () => {
    const c = clockAt({ v: 0 });
    c.observe(new Date(5000).toISOString(), 0, 0);
    c.reset();
    expect(c.synced).toBe(false);
  });
});

describe('splitDuration', () => {
  it('splits into d/h/m/s, rounding the partial second up', () => {
    const p = splitDuration(((1 * 24 + 2) * 3600 + 3 * 60 + 4) * 1000 + 100);
    expect(p).toMatchObject({ days: 1, hours: 2, minutes: 3, seconds: 5 });
  });

  it('never goes negative', () => {
    expect(splitDuration(-5000)).toEqual({ totalMs: 0, days: 0, hours: 0, minutes: 0, seconds: 0 });
  });
});
