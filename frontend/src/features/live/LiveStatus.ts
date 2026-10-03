import { ApiError, isApiError } from '@/api/errors';
import { statusSchema, type StatusResponse } from '@/api/schemas';
import { backoffDelay, withJitter, type Rng } from '@/lib/backoff';
import type { SseMessage } from './sseParser';
import { NO_STREAMING } from './streamSse';

/**
 * Keeps one person's status fresh, cheaply.
 *
 *   SSE (live) ──3 failures──▶ polling every 5–10 s (jittered) ──every ~60 s──▶ try SSE again
 *      ▲  reconnect with backoff + jitter, resuming from Last-Event-ID
 *
 * Load rules this enforces (they matter at 50,000 clients):
 *  - when SSE works there is no polling at all;
 *  - polling never runs more often than every 5 s, backs off on errors and obeys Retry-After;
 *  - every delay is jittered so a server blip doesn't make every client return in the same second;
 *  - nothing runs while the tab is hidden or the device is offline;
 *  - once the outcome is final (claimed, lost, expired, closed) all traffic stops.
 *
 * Framework-free so the timing rules are unit-tested with fake timers; useLiveStatus binds it to React.
 */

export type Connection =
  | 'connecting' // first SSE attempt
  | 'live' // SSE open
  | 'reconnecting' // SSE or poll failed, waiting to retry
  | 'polling' // SSE unavailable, checking periodically
  | 'paused' // tab hidden
  | 'offline' // browser reports no network
  | 'final' // outcome can no longer change; stopped on purpose
  | 'stopped'; // stopped by the page or by a fatal error

export interface ConnectionInfo {
  connection: Connection;
  /** How long until the next attempt, when one is scheduled. */
  retryInMs?: number;
  /** The failure that caused the current state, if any. */
  error?: ApiError;
}

export interface StreamArgs {
  lastEventId: string | null;
  signal: AbortSignal;
  onOpen: () => void;
  onMessage: (m: SseMessage) => void;
  onActivity: () => void;
  onRetry: (ms: number) => void;
}

export interface LiveDeps {
  stream: (args: StreamArgs) => Promise<void>;
  poll: (signal: AbortSignal) => Promise<StatusResponse>;
}

export interface LiveCallbacks {
  onStatus: (status: StatusResponse, source: 'sse' | 'poll') => void;
  onConnection: (info: ConnectionInfo) => void;
}

export interface LiveOptions {
  sseFailuresBeforePolling?: number;
  /** Server heartbeats arrive about every 15–20 s; silence for this long means the stream is dead. */
  heartbeatTimeoutMs?: number;
  pollMinMs?: number;
  pollSpreadMs?: number;
  pollMaxMs?: number;
  /** While polling, how often to try SSE again. */
  upgradeAfterMs?: number;
  random?: Rng;
}

/** Errors that no amount of retrying will fix. */
const FATAL = new Set(['UNAUTHENTICATED', 'FORBIDDEN', 'NOT_FOUND', 'REJECTED']);

/** Nothing about this person's outcome can change any more. */
export function isFinalStatus(s: StatusResponse): boolean {
  return s.state === 'CLAIMED' || s.state === 'LOST' || s.state === 'EXPIRED' || s.phase === 'CLOSED';
}

function toApiError(e: unknown): ApiError {
  if (isApiError(e)) return e;
  return new ApiError('UNKNOWN', e instanceof Error ? e.message : String(e), 0, undefined, undefined, e);
}

export class LiveStatus {
  private readonly o: Required<LiveOptions>;
  private running = false;
  private suspended: 'paused' | 'offline' | null = null;
  private mode: 'sse' | 'poll' = 'sse';
  private ctl: AbortController | null = null;
  private timers = new Set<ReturnType<typeof setTimeout>>();
  private watchdog: ReturnType<typeof setTimeout> | null = null;
  private sseFailures = 0;
  private pollFailures = 0;
  private lastEventId: string | null = null;
  private retryHintMs = 0;

  constructor(
    private readonly deps: LiveDeps,
    private readonly cb: LiveCallbacks,
    options: LiveOptions = {},
  ) {
    this.o = {
      sseFailuresBeforePolling: 3,
      heartbeatTimeoutMs: 45_000,
      pollMinMs: 5_000,
      pollSpreadMs: 5_000,
      pollMaxMs: 60_000,
      upgradeAfterMs: 60_000,
      random: Math.random,
      ...options,
    };
  }

  /* ------------------------------------------------------------ lifecycle */

  start(): void {
    if (this.running) return;
    this.running = true;
    this.connect();
  }

  stop(kind: 'stopped' | 'final' = 'stopped', error?: ApiError): void {
    this.running = false;
    this.suspended = null;
    this.halt();
    this.emit({ connection: kind, error });
  }

  /** Tab hidden ('paused') or device offline ('offline'): drop the connection and all timers. */
  suspend(kind: 'paused' | 'offline'): void {
    if (!this.running) return;
    this.suspended = kind;
    this.halt();
    this.emit({ connection: kind });
  }

  /** Back in view / back online: reconnect after a short random delay (so a whole room unlocking phones doesn't sync up). */
  resume(): void {
    if (!this.running || !this.suspended) return;
    this.suspended = null;
    this.sseFailures = 0;
    this.emit({ connection: 'connecting' });
    this.later(() => this.connect(), withJitter(0, 1_500, this.o.random));
  }

  get lastId(): string | null {
    return this.lastEventId;
  }

  /* --------------------------------------------------------------- SSE */

  private connect(): void {
    if (!this.active) return;
    this.halt();
    this.mode = 'sse';
    this.emit({ connection: this.sseFailures > 0 ? 'reconnecting' : 'connecting' });

    const ctl = new AbortController();
    this.ctl = ctl;
    this.armWatchdog(ctl);

    this.deps
      .stream({
        lastEventId: this.lastEventId,
        signal: ctl.signal,
        onOpen: () => {
          if (this.ctl !== ctl) return;
          this.sseFailures = 0;
          this.emit({ connection: 'live' });
        },
        onMessage: (m) => {
          if (this.ctl !== ctl) return;
          this.armWatchdog(ctl);
          this.handleMessage(m);
        },
        onActivity: () => {
          if (this.ctl === ctl) this.armWatchdog(ctl);
        },
        onRetry: (ms) => {
          this.retryHintMs = ms;
        },
      })
      .then(
        () => this.streamEnded(ctl, undefined),
        (e: unknown) => this.streamEnded(ctl, e),
      );
  }

  private handleMessage(m: SseMessage): void {
    if (m.id !== undefined) this.lastEventId = m.id;
    if (m.event !== 'status') return; // unknown event types are ignored (forward compatible)
    let json: unknown;
    try {
      json = JSON.parse(m.data);
    } catch {
      json = undefined;
    }
    const parsed = statusSchema.safeParse(json);
    if (!parsed.success) {
      console.error('[live] status event failed validation', parsed.error.issues, m.data);
      this.emit({ connection: 'live', error: new ApiError('SCHEMA_MISMATCH', 'Live update in an unexpected format', 200) });
      return;
    }
    this.cb.onStatus(parsed.data, 'sse');
    if (isFinalStatus(parsed.data)) this.stop('final');
  }

  /** The stream closed (cleanly or not). Anything we aborted ourselves has already moved on. */
  private streamEnded(ctl: AbortController, err: unknown): void {
    if (this.ctl !== ctl || !this.active) return;
    this.ctl = null;
    this.clearWatchdog();
    this.sseFailed(err === undefined ? undefined : toApiError(err));
  }

  private sseFailed(error: ApiError | undefined): void {
    if (error && FATAL.has(error.code)) return this.stop('stopped', error);
    this.sseFailures++;
    const cannotStream = error?.details?.reason === NO_STREAMING || error?.code === 'SCHEMA_MISMATCH';
    if (cannotStream || this.sseFailures >= this.o.sseFailuresBeforePolling) return this.startPolling(error);

    const delay = backoffDelay(
      this.sseFailures - 1,
      { baseMs: Math.max(2_000, this.retryHintMs), maxMs: 30_000, retryAfterMs: error?.retryAfterMs },
      this.o.random,
    );
    this.emit({ connection: 'reconnecting', retryInMs: delay, error });
    this.later(() => this.connect(), delay);
  }

  private armWatchdog(ctl: AbortController): void {
    this.clearWatchdog();
    this.watchdog = setTimeout(() => {
      if (this.ctl !== ctl) return;
      // Connected but silent: proxies sometimes hold a dead socket open. Treat as a failure.
      this.ctl = null;
      ctl.abort();
      this.sseFailed(new ApiError('TIMEOUT', 'Live stream went quiet', 0));
    }, this.o.heartbeatTimeoutMs);
  }

  private clearWatchdog(): void {
    if (this.watchdog) clearTimeout(this.watchdog);
    this.watchdog = null;
  }

  /* ----------------------------------------------------------- polling */

  private startPolling(error?: ApiError): void {
    this.halt();
    this.mode = 'poll';
    this.pollFailures = 0;
    const first = withJitter(1_000, 2_000, this.o.random);
    this.emit({ connection: 'polling', retryInMs: first, error });
    this.later(() => void this.pollOnce(), first);
    this.later(() => this.tryUpgrade(), withJitter(this.o.upgradeAfterMs, 15_000, this.o.random));
  }

  private async pollOnce(): Promise<void> {
    if (!this.active || this.mode !== 'poll') return;
    const ctl = new AbortController();
    this.ctl = ctl;
    try {
      const status = await this.deps.poll(ctl.signal);
      if (this.ctl !== ctl || !this.active) return;
      this.ctl = null;
      this.pollFailures = 0;
      this.cb.onStatus(status, 'poll');
      if (isFinalStatus(status)) return this.stop('final');
      const next = withJitter(this.o.pollMinMs, this.o.pollSpreadMs, this.o.random);
      this.emit({ connection: 'polling', retryInMs: next });
      this.later(() => void this.pollOnce(), next);
    } catch (e) {
      if (this.ctl !== ctl || !this.active) return;
      this.ctl = null;
      const error = toApiError(e);
      if (FATAL.has(error.code)) return this.stop('stopped', error);
      this.pollFailures++;
      const delay = backoffDelay(
        this.pollFailures,
        { baseMs: this.o.pollMinMs, maxMs: this.o.pollMaxMs, floorMs: this.o.pollMinMs, retryAfterMs: error.retryAfterMs },
        this.o.random,
      );
      this.emit({ connection: 'reconnecting', retryInMs: delay, error });
      this.later(() => void this.pollOnce(), delay);
    }
  }

  /** Periodically try to get back onto SSE. One failure drops straight back to polling. */
  private tryUpgrade(): void {
    if (!this.active || this.mode !== 'poll') return;
    this.sseFailures = this.o.sseFailuresBeforePolling - 1;
    this.connect();
  }

  /* ----------------------------------------------------------- plumbing */

  private get active(): boolean {
    return this.running && this.suspended === null;
  }

  private later(fn: () => void, ms: number): void {
    const t = setTimeout(() => {
      this.timers.delete(t);
      fn();
    }, ms);
    this.timers.add(t);
  }

  /** Cancel the in-flight request/stream and every pending timer. */
  private halt(): void {
    for (const t of this.timers) clearTimeout(t);
    this.timers.clear();
    this.clearWatchdog();
    const ctl = this.ctl;
    this.ctl = null;
    ctl?.abort();
  }

  private emit(info: ConnectionInfo): void {
    this.cb.onConnection(info);
  }
}
