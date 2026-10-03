import { ApiError, CLIENT_ERROR_CODES, SERVER_ERROR_CODES, describeError, errorMessages, isErrorCode } from './errors';

describe('error map', () => {
  it('has friendly copy for every server and client error code', () => {
    for (const code of [...SERVER_ERROR_CODES, ...CLIENT_ERROR_CODES]) {
      const copy = errorMessages[code];
      expect(copy, code).toBeDefined();
      expect(copy.title.length, code).toBeGreaterThan(3);
      expect(copy.body.length, code).toBeGreaterThan(10);
    }
  });

  it('never shouts: no exclamation marks, no raw codes in user-facing copy', () => {
    for (const [code, copy] of Object.entries(errorMessages)) {
      expect(copy.title + copy.body, code).not.toMatch(/!/);
      expect(copy.title + copy.body, code).not.toMatch(/[A-Z]{3,}_[A-Z_]+/);
    }
  });

  it('recognises known codes only', () => {
    expect(isErrorCode('RATE_LIMITED')).toBe(true);
    expect(isErrorCode('NETWORK_ERROR')).toBe(true);
    expect(isErrorCode('WHATEVER')).toBe(false);
  });
});

describe('describeError', () => {
  it('maps an ApiError to its copy', () => {
    const e = describeError(new ApiError('WINDOW_NOT_OPEN', 'nope', 409));
    expect(e.code).toBe('WINDOW_NOT_OPEN');
    expect(e.title).toMatch(/hasn't opened/i);
    expect(e.retryable).toBe(false);
  });

  it('adds a countdown to RATE_LIMITED, rounded up to whole seconds', () => {
    const e = describeError(new ApiError('RATE_LIMITED', 'slow down', 429, undefined, 7200));
    expect(e.body).toMatch(/try again in 8 seconds/i);
    expect(e.retryAfterMs).toBe(7200);
    expect(describeError(new ApiError('RATE_LIMITED', 'x', 429, undefined, 400)).body).toMatch(/in 1 second\./);
  });

  it('shows the server message only for validation errors', () => {
    expect(describeError(new ApiError('VALIDATION_ERROR', 'That code is not right', 400)).detail).toBe('That code is not right');
    expect(describeError(new ApiError('INTERNAL', 'stack trace here', 500)).detail).toBeUndefined();
  });

  it('falls back by status for codes we do not know', () => {
    expect(describeError(new ApiError('SOMETHING_NEW', 'x', 502)).code).toBe('INTERNAL');
    expect(describeError(new ApiError('SOMETHING_NEW', 'x', 418)).code).toBe('UNKNOWN');
  });

  it('copes with non-ApiError values', () => {
    expect(describeError(new Error('boom')).code).toBe('UNKNOWN');
    expect(describeError('lol').code).toBe('UNKNOWN');
  });
});
