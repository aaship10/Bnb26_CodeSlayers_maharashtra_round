import type { ServerResponse } from 'node:http';
import type { EventDef } from './events';
import type { User, World } from './world';

interface Client {
  user: User;
  ev: EventDef;
  res: ServerResponse;
}

interface LogEntry {
  id: number;
  key: string;
  data: string;
}

const MAX_LOG = 200;
const HEARTBEAT_TICKS = 15;

/**
 * Server-sent events with resume. Every state change a person sees is appended
 * to a per-(user, event) log with a monotonically increasing id; a reconnect
 * with Last-Event-ID replays whatever it missed. The log is the proof that the
 * client really does resume instead of just re-fetching.
 */
export class SseHub {
  private clients = new Set<Client>();
  private logs = new Map<string, { nextId: number; entries: LogEntry[] }>();
  private ticks = 0;

  constructor(private readonly w: World) {}

  get clientCount(): number {
    return this.clients.size;
  }

  private logFor(user: User, ev: EventDef) {
    const k = `${user.id}:${ev.id}`;
    let log = this.logs.get(k);
    if (!log) {
      log = { nextId: 1, entries: [] };
      this.logs.set(k, log);
    }
    return log;
  }

  /** Append the current status to the log if it differs from the last entry. Returns the new entry, if any. */
  private capture(user: User, ev: EventDef): LogEntry | undefined {
    const status = this.w.status(user, ev);
    const key = JSON.stringify(status);
    const log = this.logFor(user, ev);
    const last = log.entries[log.entries.length - 1];
    if (last && last.key === key) return undefined;
    const entry: LogEntry = {
      id: log.nextId++,
      key,
      data: JSON.stringify({ ...status, server_now: this.w.clock.iso() }),
    };
    log.entries.push(entry);
    if (log.entries.length > MAX_LOG) log.entries.shift();
    return entry;
  }

  private write(res: ServerResponse, entry: LogEntry) {
    res.write(`id: ${entry.id}\nevent: status\ndata: ${entry.data}\n\n`);
  }

  open(res: ServerResponse, user: User, ev: EventDef, lastEventId: number | null): void {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
    });
    res.write('retry: 3000\n\n');

    this.capture(user, ev);
    const log = this.logFor(user, ev);
    const toSend =
      lastEventId === null
        ? log.entries.slice(-1)
        : log.entries.filter((e) => e.id > lastEventId);
    for (const e of toSend) this.write(res, e);

    const client: Client = { user, ev, res };
    this.clients.add(client);
    res.on('close', () => this.clients.delete(client));
  }

  /** Called every second: pushes status changes caused by the clock or by actions. */
  tick(): void {
    this.ticks++;
    for (const c of this.clients) {
      const entry = this.capture(c.user, c.ev);
      if (entry) this.write(c.res, entry);
      else if (this.ticks % HEARTBEAT_TICKS === 0) c.res.write(': ping\n\n');
    }
  }

  /** Simulate a dropped connection. Clients should reconnect and resume. */
  dropAll(): number {
    const n = this.clients.size;
    for (const c of this.clients) c.res.destroy();
    this.clients.clear();
    return n;
  }
}

