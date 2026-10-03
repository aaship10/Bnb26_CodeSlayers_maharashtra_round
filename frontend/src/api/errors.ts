/**
 * One place that turns error codes into human words.
 * Copy rules: calm, specific, never blame the user, never promise what we
 * can't guarantee (a failed request is not proof anything was saved).
 */

export const SERVER_ERROR_CODES = [
  'WINDOW_NOT_OPEN',
  'WINDOW_CLOSED',
  'NOT_WINNER',
  'HOLD_EXPIRED',
  'ALREADY_CLAIMED',
  'UNAUTHENTICATED',
  'FORBIDDEN',
  'RATE_LIMITED',
  'CHALLENGE_REQUIRED',
  'REJECTED',
  'VALIDATION_ERROR',
  'NOT_FOUND',
  'INTERNAL',
] as const;

/** Raised by the client itself, not by the server. */
export const CLIENT_ERROR_CODES = ['NETWORK_ERROR', 'TIMEOUT', 'SCHEMA_MISMATCH', 'UNKNOWN'] as const;

export type ServerErrorCode = (typeof SERVER_ERROR_CODES)[number];
export type ClientErrorCode = (typeof CLIENT_ERROR_CODES)[number];
export type ErrorCode = ServerErrorCode | ClientErrorCode;

export class ApiError extends Error {
  readonly name = 'ApiError';
  constructor(
    /** A known ErrorCode, or whatever string the server sent (kept for debugging). */
    readonly code: string,
    message: string,
    /** HTTP status; 0 when no response was received. */
    readonly status: number,
    readonly details?: Record<string, unknown>,
    /** Parsed from details.retry_after_ms or the Retry-After header. */
    readonly retryAfterMs?: number,
    readonly cause?: unknown,
  ) {
    super(message);
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

export function isErrorCode(code: string): code is ErrorCode {
  return (SERVER_ERROR_CODES as readonly string[]).includes(code) || (CLIENT_ERROR_CODES as readonly string[]).includes(code);
}

export type Tone = 'info' | 'warn' | 'error';

export interface ErrorCopy {
  title: string;
  body: string;
  tone: Tone;
  /** Would retrying the same thing plausibly work? */
  retryable: boolean;
}

export const errorMessages: Record<ErrorCode, ErrorCopy> = {
  WINDOW_NOT_OPEN: {
    title: "The window hasn't opened yet",
    body: 'Entries open at the time shown on this page. There is no advantage to being early: everyone who enters during the window has the same chance.',
    tone: 'info',
    retryable: false,
  },
  WINDOW_CLOSED: {
    title: 'The entry window has closed',
    body: 'Entries are closed for this drop. If you entered in time, your result will appear on your status page.',
    tone: 'info',
    retryable: false,
  },
  NOT_WINNER: {
    title: 'No seat is being held for you',
    body: 'Seats are only held for people picked in the draw. Your status page shows where you stand.',
    tone: 'info',
    retryable: false,
  },
  HOLD_EXPIRED: {
    title: 'Your hold ran out',
    body: 'The time to claim this seat has passed and it has gone back to the pool. Thanks for taking part.',
    tone: 'warn',
    retryable: false,
  },
  ALREADY_CLAIMED: {
    title: "You've already claimed a seat",
    body: 'Your ticket is safe. You can find it on your ticket page.',
    tone: 'info',
    retryable: false,
  },
  UNAUTHENTICATED: {
    title: 'Please sign in',
    body: 'Your session has ended. Sign in again and carry on where you left off.',
    tone: 'info',
    retryable: false,
  },
  FORBIDDEN: {
    title: "That isn't available to you",
    body: "Your account doesn't have access to this.",
    tone: 'warn',
    retryable: false,
  },
  RATE_LIMITED: {
    title: 'Let’s slow down a little',
    body: 'We are getting a lot of requests from your connection. You can try again in a moment.',
    tone: 'warn',
    retryable: true,
  },
  CHALLENGE_REQUIRED: {
    title: 'One quick check',
    body: 'We need to confirm you are a person. This only takes a moment.',
    tone: 'info',
    retryable: true,
  },
  REJECTED: {
    title: "We couldn't accept this request",
    body: 'If you are a real person, wait a few minutes and try again, or use a different browser.',
    tone: 'warn',
    retryable: false,
  },
  VALIDATION_ERROR: {
    title: 'Check the details and try again',
    body: "Something in what you sent didn't look right.",
    tone: 'warn',
    retryable: false,
  },
  NOT_FOUND: {
    title: "We couldn't find that",
    body: 'It may have moved, or it may never have existed.',
    tone: 'info',
    retryable: false,
  },
  INTERNAL: {
    title: 'Something went wrong on our side',
    body: 'Nothing you did caused this. Please try again in a moment.',
    tone: 'error',
    retryable: true,
  },
  NETWORK_ERROR: {
    title: "Can't reach the server",
    body: 'Check your connection. We will keep trying in the background.',
    tone: 'warn',
    retryable: true,
  },
  TIMEOUT: {
    title: 'That took too long',
    body: "The server didn't answer in time. Please try again.",
    tone: 'warn',
    retryable: true,
  },
  SCHEMA_MISMATCH: {
    title: 'We got an unexpected answer',
    body: 'The server replied in a format this app does not understand. That is a bug on our side, not something you did.',
    tone: 'error',
    retryable: false,
  },
  UNKNOWN: {
    title: 'Something unexpected happened',
    body: 'Please try again. If it keeps happening, tell the organisers.',
    tone: 'error',
    retryable: true,
  },
};

export interface FriendlyError extends ErrorCopy {
  code: ErrorCode;
  /** Server-provided detail, shown as a secondary line when it adds information. */
  detail?: string;
  retryAfterMs?: number;
}

/** Whole seconds, rounded up, never below 1: "try again in 8 seconds". */
export function retrySeconds(ms: number): number {
  return Math.max(1, Math.ceil(ms / 1000));
}

export function describeError(error: unknown): FriendlyError {
  if (isApiError(error)) {
    const code: ErrorCode = isErrorCode(error.code) ? error.code : error.status >= 500 ? 'INTERNAL' : 'UNKNOWN';
    const copy = errorMessages[code];
    let body = copy.body;
    if (code === 'RATE_LIMITED' && error.retryAfterMs) {
      const s = retrySeconds(error.retryAfterMs);
      body = `${copy.body} Try again in ${s} second${s === 1 ? '' : 's'}.`;
    }
    // A server message is only worth showing when it is about *what* was wrong
    // (validation), not a generic restatement of the code.
    const detail = code === 'VALIDATION_ERROR' && error.message ? error.message : undefined;
    return { ...copy, body, code, detail, retryAfterMs: error.retryAfterMs };
  }
  return { ...errorMessages.UNKNOWN, code: 'UNKNOWN' };
}
