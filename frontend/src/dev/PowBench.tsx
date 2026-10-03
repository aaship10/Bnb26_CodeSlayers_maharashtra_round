import { useEffect, useRef, useState } from 'react';
import { solvePowInWorker } from '@/features/challenge/solvePow';
import { Alert } from '@/ui/Alert';
import { Button } from '@/ui/Button';
import { formatCount } from '@/lib/format';

interface Result {
  rate: number;
  seconds: number;
}

/**
 * Dev page: measure proof-of-work throughput on THIS device. Open it on a
 * mid-range phone over the LAN (npm run dev:lan, then http://<your-ip>:5173/__dev/pow-bench)
 * to pick a sensible difficulty for the demo. It also reports whether the page is
 * a secure context: over plain http crypto.subtle is missing, which is exactly why
 * the solver is pure JS.
 */
export function PowBench() {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const [live, setLive] = useState(0);
  const ctrl = useRef<AbortController | null>(null);
  useEffect(() => () => ctrl.current?.abort(), []);

  const run = async () => {
    setRunning(true);
    setResult(null);
    const c = new AbortController();
    ctrl.current = c;
    const t0 = performance.now();
    let hashes = 0;
    // difficulty 64 can't be met, so this just measures raw attempts per second
    const stop = setTimeout(() => c.abort(), 3000);
    try {
      await solvePowInWorker(
        { prefix: 'bench.' + 'x'.repeat(40), difficultyBits: 64 },
        {
          signal: c.signal,
          onProgress: (h) => {
            hashes = h;
            setLive(h);
          },
        },
      );
    } catch {
      /* aborted on purpose */
    }
    clearTimeout(stop);
    const seconds = (performance.now() - t0) / 1000;
    setResult({ rate: hashes / seconds, seconds });
    setRunning(false);
  };

  const rows = [16, 18, 20, 22, 24];
  return (
    <div className="mx-auto max-w-xl space-y-6">
      <h1 className="font-display text-3xl">Proof-of-work benchmark</h1>
      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1 text-sm">
        <dt className="text-ink-3">Secure context</dt>
        <dd>{String(window.isSecureContext)}</dd>
        <dt className="text-ink-3">crypto.subtle</dt>
        <dd>{typeof crypto !== 'undefined' && crypto.subtle ? 'available' : 'missing (expected on http://<lan-ip>)'}</dd>
        <dt className="text-ink-3">Web Workers</dt>
        <dd>{typeof Worker !== 'undefined' ? 'available' : 'missing: main-thread fallback'}</dd>
        <dt className="text-ink-3">CPU threads</dt>
        <dd>{navigator.hardwareConcurrency}</dd>
      </dl>

      <Button onClick={() => void run()} loading={running}>
        {running ? `Hashing… ${formatCount(live)}` : 'Run a 3 second benchmark'}
      </Button>

      {result && (
        <Alert tone="success" title={`${formatCount(Math.round(result.rate))} attempts per second`}>
          <table className="mt-2 w-full text-left text-sm">
            <thead>
              <tr className="text-ink-3">
                <th className="font-normal">bits</th>
                <th className="font-normal">average attempts</th>
                <th className="font-normal">average time here</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((b) => (
                <tr key={b} className="tnum border-t border-rule">
                  <td>{b}</td>
                  <td>{formatCount(2 ** b)}</td>
                  <td>{(2 ** b / result.rate).toFixed(2)} s</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Alert>
      )}
    </div>
  );
}
