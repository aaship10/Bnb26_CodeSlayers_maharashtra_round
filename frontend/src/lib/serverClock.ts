/**
 * Server-time offset. The client clock is never authoritative: it is used only
 * to *display* countdowns. We estimate offset = serverTime - localTime from
 * every response that carries `server_now`, assuming a symmetric round trip,
 * and trust the sample with the lowest round-trip time (least uncertainty).
 */
export interface ClockSample {
  offsetMs: number;
  rttMs: number;
  at: number;
}

const MAX_SAMPLES = 8;
const MAX_SAMPLE_AGE_MS = 2 * 60_000;

export class ServerClock {
  private samples: ClockSample[] = [];

  constructor(private readonly localNow: () => number = () => Date.now()) {}

  /**
   * @param serverNowIso server_now from the response body
   * @param sentAtMs local time just before the request was sent
   * @param receivedAtMs local time when the response arrived
   */
  observe(serverNowIso: string, sentAtMs: number, receivedAtMs: number): void {
    const serverMs = Date.parse(serverNowIso);
    if (!Number.isFinite(serverMs)) return;
    const rttMs = Math.max(0, receivedAtMs - sentAtMs);
    // The server stamped server_now roughly halfway through the round trip.
    const offsetMs = serverMs - (sentAtMs + rttMs / 2);
    // Drop samples old enough that clock drift (or a server-time jump) could have made them wrong.
    this.samples = this.samples.filter((s) => receivedAtMs - s.at <= MAX_SAMPLE_AGE_MS);
    this.samples.push({ offsetMs, rttMs, at: receivedAtMs });
    if (this.samples.length > MAX_SAMPLES) this.samples.shift();
  }

  get synced(): boolean {
    return this.samples.length > 0;
  }

  /** Offset (server minus local) from the lowest-RTT recent sample; 0 until synced. */
  get offsetMs(): number {
    if (this.samples.length === 0) return 0;
    let best = this.samples[0]!;
    for (const s of this.samples) if (s.rttMs < best.rttMs) best = s;
    return best.offsetMs;
  }

  /** Best guess of the server's current time, in epoch ms. */
  now(): number {
    return this.localNow() + this.offsetMs;
  }

  /** Milliseconds from (estimated) server-now until the given ISO time. Negative if past. */
  msUntil(iso: string): number {
    return Date.parse(iso) - this.now();
  }

  reset(): void {
    this.samples = [];
  }
}

/** Shared instance used by the API client and the UI. */
export const serverClock = new ServerClock();

export interface CountdownParts {
  totalMs: number;
  days: number;
  hours: number;
  minutes: number;
  seconds: number;
}

/** Split a duration into display parts. Clamps at zero: a countdown never goes negative. */
export function splitDuration(ms: number): CountdownParts {
  const totalMs = Math.max(0, ms);
  const totalSeconds = Math.ceil(totalMs / 1000);
  return {
    totalMs,
    days: Math.floor(totalSeconds / 86400),
    hours: Math.floor((totalSeconds % 86400) / 3600),
    minutes: Math.floor((totalSeconds % 3600) / 60),
    seconds: totalSeconds % 60,
  };
}
