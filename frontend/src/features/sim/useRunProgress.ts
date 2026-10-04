import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { backoffDelay } from '@/lib/backoff';
import { streamSse } from '@/features/live/streamSse';
import { simApi, simKeys } from './simApi';
import { runSchema, snapshotSchema, type Run, type Snapshot } from './schemas';

export interface RunProgress {
  run: Run | null;
  snapshots: Snapshot[];
  /** 'sse' while streaming, 'poll' after falling back, 'idle' when finished. */
  via: 'sse' | 'poll' | 'idle';
  error: string | null;
}

const TERMINAL = new Set(['done', 'failed', 'cancelled']);
const MAX_SNAPSHOTS = 120;

/**
 * Live progress for one run: SSE (resuming with Last-Event-ID) with a polling
 * fallback after repeated failures. Stops for good once the run is terminal.
 */
export function useRunProgress(runId: string): RunProgress {
  const queryClient = useQueryClient();
  const [state, setState] = useState<RunProgress>({ run: null, snapshots: [], via: 'sse', error: null });
  const lastId = useRef<string | null>(null);

  useEffect(() => {
    let stopped = false;
    let ctl: AbortController | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;

    const apply = (run: Run) => {
      setState((s) => ({ ...s, run, error: null }));
      if (TERMINAL.has(run.status)) {
        stopped = true;
        ctl?.abort();
        setState((s) => ({ ...s, via: 'idle' }));
        void queryClient.invalidateQueries({ queryKey: simKeys.runs });
        if (run.status === 'done') void queryClient.invalidateQueries({ queryKey: simKeys.results(runId) });
      }
    };

    const poll = async () => {
      if (stopped) return;
      try {
        apply(await simApi.run(runId));
        failures = 0;
      } catch (e) {
        failures++;
        setState((s) => ({ ...s, error: e instanceof Error ? e.message : String(e) }));
      }
      if (!stopped) timer = setTimeout(() => void poll(), backoffDelay(failures, { baseMs: 2_000, maxMs: 30_000, floorMs: 2_000 }));
    };

    const stream = async () => {
      if (stopped) return;
      ctl = new AbortController();
      try {
        await streamSse({
          url: simApi.streamUrl(runId),
          headers: {},
          lastEventId: lastId.current,
          signal: ctl.signal,
          onOpen: () => {
            failures = 0;
          },
          onMessage: (m) => {
            if (m.id !== undefined) lastId.current = m.id;
            let data: unknown;
            try {
              data = JSON.parse(m.data);
            } catch {
              return;
            }
            if (m.event === 'status' || m.event === 'progress') {
              const r = runSchema.partial({ status: true, progress: true }).safeParse(data);
              if (r.success) {
                setState((s) => {
                  const merged = { ...(s.run ?? { status: 'queued', progress: 0 }), ...r.data } as Run;
                  return { ...s, run: merged, error: null };
                });
                const full = runSchema.safeParse(data);
                if (full.success && TERMINAL.has(full.data.status)) apply(full.data);
              }
            } else if (m.event === 'snapshot') {
              const snap = snapshotSchema.safeParse(data);
              if (snap.success) setState((s) => ({ ...s, snapshots: [...s.snapshots, snap.data].slice(-MAX_SNAPSHOTS) }));
            }
          },
        });
        // Server closed the stream: if the run isn't over, reconnect.
        if (!stopped) throw new Error('stream closed');
      } catch {
        if (stopped || ctl?.signal.aborted) return;
        failures++;
        if (failures >= 3) {
          setState((s) => ({ ...s, via: 'poll' }));
          return void poll();
        }
        timer = setTimeout(() => void stream(), backoffDelay(failures - 1, { baseMs: 1_000, maxMs: 8_000 }));
      }
    };

    // Start from the run's current status (cheap), then follow it live.
    simApi
      .run(runId)
      .then((r) => {
        apply(r);
        if (!stopped) void stream();
      })
      .catch((e: unknown) => {
        setState((s) => ({ ...s, error: e instanceof Error ? e.message : String(e) }));
        if (!stopped) void stream();
      });

    return () => {
      stopped = true;
      ctl?.abort();
      clearTimeout(timer);
    };
  }, [runId, queryClient]);

  return state;
}
