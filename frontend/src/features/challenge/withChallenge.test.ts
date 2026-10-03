import { ApiError } from '@/api/errors';
import { withChallenges, type ChallengeHandler } from './withChallenge';

const powChallenge = (id = 'ch_1') => ({
  id,
  type: 'pow',
  expires_at: '2026-11-01T10:02:00.000Z',
  pow: { algo: 'sha256-lzb', prefix: 'fd1.x', difficulty_bits: 12 },
});

const challengeError = (challenge: unknown) => new ApiError('CHALLENGE_REQUIRED', 'Challenge required', 403, { challenge });

function handler(impl?: ChallengeHandler['solve']) {
  const solve = vi.fn(impl ?? (async (c) => ({ id: c.id, solution: '777' })));
  return { handler: { solve } as ChallengeHandler, solve };
}

const signal = () => new AbortController().signal;

describe('withChallenges', () => {
  it('passes straight through when no challenge is needed', async () => {
    const { handler: h, solve } = handler();
    const attempt = vi.fn().mockResolvedValue('ok');
    await expect(withChallenges(attempt, h, { signal: signal() })).resolves.toBe('ok');
    expect(attempt).toHaveBeenCalledTimes(1);
    expect(attempt).toHaveBeenCalledWith(undefined);
    expect(solve).not.toHaveBeenCalled();
  });

  it('solves the challenge and repeats the SAME request carrying the solution', async () => {
    const { handler: h, solve } = handler();
    const attempt = vi.fn().mockRejectedValueOnce(challengeError(powChallenge('ch_9'))).mockResolvedValueOnce('entered');

    await expect(withChallenges(attempt, h, { signal: signal() })).resolves.toBe('entered');

    expect(solve).toHaveBeenCalledTimes(1);
    expect(solve.mock.calls[0]![0]).toMatchObject({ id: 'ch_9', type: 'pow' });
    expect(attempt).toHaveBeenNthCalledWith(1, undefined);
    expect(attempt).toHaveBeenNthCalledWith(2, { id: 'ch_9', solution: '777' });
  });

  it('handles a fresh challenge after a failed solution, then succeeds', async () => {
    const { handler: h, solve } = handler();
    const attempt = vi
      .fn()
      .mockRejectedValueOnce(challengeError(powChallenge('a')))
      .mockRejectedValueOnce(challengeError(powChallenge('b')))
      .mockResolvedValueOnce('ok');
    await expect(withChallenges(attempt, h, { signal: signal() })).resolves.toBe('ok');
    expect(solve.mock.calls.map((c) => c[0].id)).toEqual(['a', 'b']);
  });

  it('gives up after maxRounds instead of looping forever', async () => {
    const { handler: h, solve } = handler();
    const attempt = vi.fn().mockRejectedValue(challengeError(powChallenge()));
    await expect(withChallenges(attempt, h, { signal: signal(), maxRounds: 2 })).rejects.toMatchObject({ code: 'CHALLENGE_REQUIRED' });
    expect(solve).toHaveBeenCalledTimes(2);
    expect(attempt).toHaveBeenCalledTimes(3);
  });

  it('does not touch other errors', async () => {
    const { handler: h, solve } = handler();
    const boom = new ApiError('RATE_LIMITED', 'slow', 429);
    await expect(withChallenges(vi.fn().mockRejectedValue(boom), h, { signal: signal() })).rejects.toBe(boom);
    expect(solve).not.toHaveBeenCalled();
  });

  it('fails visibly (SCHEMA_MISMATCH) when the 403 carries no usable challenge', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const { handler: h, solve } = handler();
    const attempt = vi.fn().mockRejectedValue(challengeError({ id: 'x', type: 'pow' })); // pow params missing
    await expect(withChallenges(attempt, h, { signal: signal() })).rejects.toMatchObject({ code: 'SCHEMA_MISMATCH' });
    expect(solve).not.toHaveBeenCalled();
    spy.mockRestore();
  });

  it('propagates a cancelled solve (navigation away) without retrying', async () => {
    const abort = new DOMException('cancelled', 'AbortError');
    const { handler: h } = handler(async () => {
      throw abort;
    });
    const attempt = vi.fn().mockRejectedValue(challengeError(powChallenge()));
    await expect(withChallenges(attempt, h, { signal: signal() })).rejects.toBe(abort);
    expect(attempt).toHaveBeenCalledTimes(1);
  });
});
