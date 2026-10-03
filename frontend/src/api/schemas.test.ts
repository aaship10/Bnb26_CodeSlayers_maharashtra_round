import { challengeSchema, enterResponseSchema, eventSchema, statusSchema } from './schemas';

const NOW = '2026-11-01T10:00:00.000Z';

describe('schemas', () => {
  it('accepts a valid event', () => {
    const ok = eventSchema.safeParse({
      id: 'e1',
      name: 'Night',
      phase: 'OPEN',
      mode: 'LOTTERY',
      inventory: 500,
      window_opens_at: NOW,
      window_closes_at: '2026-11-01T10:30:00.000Z',
      claim_ttl_s: 600,
      server_now: NOW,
    });
    expect(ok.success).toBe(true);
  });

  it('insists on UTC timestamps with a Z', () => {
    const bad = enterResponseSchema.safeParse({ state: 'ENTERED', entered_at: '2026-11-01T10:00:00+05:30', already_entered: false });
    expect(bad.success).toBe(false);
  });

  it('rejects unknown phases and states so contract drift is loud', () => {
    expect(statusSchema.safeParse({ state: 'MAYBE', phase: 'OPEN', server_now: NOW }).success).toBe(false);
    expect(statusSchema.safeParse({ state: 'ENTERED', phase: 'LUNCH', server_now: NOW }).success).toBe(false);
  });

  it('a pre-draw status carries no rank or draw fields by construction', () => {
    const parsed = statusSchema.parse({ state: 'ENTERED', phase: 'OPEN', server_now: NOW });
    expect(parsed.waitlist_position).toBeUndefined();
    expect(parsed.seat_no).toBeUndefined();
  });

  it('never exposes simulator ground truth: sim_label is stripped on parse', () => {
    const parsed = statusSchema.parse({ state: 'ENTERED', phase: 'OPEN', server_now: NOW, sim_label: 'bot' });
    expect(parsed).not.toHaveProperty('sim_label');
    expect(JSON.stringify(parsed)).not.toMatch(/sim_label/);
  });

  it('requires pow params on a pow challenge', () => {
    const base = { id: 'c1', type: 'pow', expires_at: NOW };
    expect(challengeSchema.safeParse(base).success).toBe(false);
    expect(
      challengeSchema.safeParse({ ...base, pow: { algo: 'sha256-lzb', prefix: 'abc', difficulty_bits: 18 } }).success,
    ).toBe(true);
    expect(challengeSchema.safeParse({ id: 'c2', type: 'captcha', expires_at: NOW }).success).toBe(false);
  });
});
