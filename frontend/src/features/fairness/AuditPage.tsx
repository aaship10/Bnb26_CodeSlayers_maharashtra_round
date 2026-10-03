import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useInfiniteQuery } from '@tanstack/react-query';
import { CircleCheck, CircleX, Link2 } from 'lucide-react';
import { apiClient, describeError } from '@/api';
import { cx } from '@/lib/cx';
import { formatDateTime } from '@/lib/format';
import { useDocumentTitle } from '@/lib/useDocumentTitle';
import { Alert } from '@/ui/Alert';
import { Badge } from '@/ui/Badge';
import { Button } from '@/ui/Button';
import { Skeleton } from '@/ui/Skeleton';
import { verifyChain, type ChainCheck } from './auditVerify';
import { bestCrypto } from './cryptoImpl';
import { useFairness } from './FairnessPage';
import { HashValue } from './HashValue';
import { auditPageSchema, auditVerifySchema, type AuditRecord, type AuditVerify } from './schemas';

const PAGE = 50;
const MAX_PAGES = 200; // 10,000 records: far beyond a single event, and a hard stop for a misbehaving API

const short = (h: string) => `${h.slice(0, 8)}…${h.slice(-6)}`;

function fetchPage(id: string, fromSeq: number, signal?: AbortSignal) {
  return apiClient.request(`/events/${encodeURIComponent(id)}/audit?from_seq=${fromSeq}&limit=${PAGE}`, { schema: auditPageSchema, auth: false, signal });
}

interface CheckResult {
  browser: ChainCheck;
  server: AuditVerify | null;
  serverError: string | null;
}

function RecordCard({ r, isHead, check }: { r: AuditRecord; isHead: boolean; check?: { ok: boolean; reason?: string } }) {
  return (
    <li
      className={cx(
        'rounded-md border-2 bg-paper-2 p-4',
        check && !check.ok ? 'border-tomato-deep' : 'border-ink',
        isHead && 'ring-4 ring-sun',
      )}
      data-testid={`audit-${r.seq}`}
      data-check={check ? (check.ok ? 'ok' : 'bad') : 'unchecked'}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="tnum grid size-8 place-items-center rounded-full border-2 border-ink bg-paper font-display text-sm font-bold">{r.seq}</span>
        <span className="font-mono text-sm font-bold">{r.type}</span>
        {isHead && <Badge tone="sun">Head</Badge>}
        <span className="ml-auto text-xs text-ink-3">{formatDateTime(r.ts)}</span>
        {check &&
          (check.ok ? (
            <CircleCheck className="size-5 text-pine" aria-label="Links correctly" />
          ) : (
            <CircleX className="size-5 text-tomato-deep" aria-label="Breaks the chain" />
          ))}
      </div>
      {check && !check.ok && <p className="mt-2 text-sm font-semibold text-tomato-deep">This record breaks the chain: {check.reason}.</p>}
      <details className="mt-2">
        <summary className="cursor-pointer text-sm text-ink-2">Payload</summary>
        <pre className="mt-2 overflow-x-auto rounded-sm bg-paper-3 p-3 font-mono text-xs leading-relaxed">{JSON.stringify(r.payload, null, 2)}</pre>
      </details>
      <dl className="mt-2 grid gap-1 text-xs sm:grid-cols-[auto_1fr] sm:gap-x-3">
        <dt className="text-ink-3">Previous</dt>
        <dd className="break-all font-mono">{short(r.prev_hash)}</dd>
        <dt className="text-ink-3">Hash</dt>
        <dd>
          <HashValue value={r.hash} label={`hash of record ${r.seq}`} />
        </dd>
      </dl>
    </li>
  );
}

export function Component() {
  const { id = '' } = useParams();
  useDocumentTitle('Audit log');
  const fairness = useFairness(id);
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<CheckResult | null>(null);
  const [checkError, setCheckError] = useState<string | null>(null);

  const pages = useInfiniteQuery({
    queryKey: ['events', id, 'audit'],
    queryFn: ({ pageParam, signal }) => fetchPage(id, pageParam, signal),
    initialPageParam: 1,
    getNextPageParam: (last) => last.next_from_seq ?? undefined,
    enabled: !!id,
  });

  const records = pages.data?.pages.flatMap((p) => p.records) ?? [];
  const head = pages.data?.pages[pages.data.pages.length - 1]?.head ?? null;
  const checks = new Map(result?.browser.records.map((c) => [c.seq, c]) ?? []);

  /** Load every page, recompute the chain here, and compare with what the server says. */
  const check = async () => {
    setChecking(true);
    setCheckError(null);
    setResult(null);
    try {
      const all: AuditRecord[] = [];
      let from: number | null = 1;
      for (let i = 0; from !== null && i < MAX_PAGES; i++) {
        const page = await fetchPage(id, from);
        all.push(...page.records);
        from = page.next_from_seq;
      }
      const crypto = bestCrypto();
      const browser = await verifyChain(all, (b) => crypto.sha256(b));
      let server: AuditVerify | null = null;
      let serverError: string | null = null;
      try {
        server = await apiClient.request(`/events/${encodeURIComponent(id)}/audit/verify`, { schema: auditVerifySchema, auth: false });
      } catch (e) {
        serverError = describeError(e).title;
      }
      setResult({ browser, server, serverError });
      void pages.refetch();
    } catch (e) {
      setCheckError(describeError(e).body);
    } finally {
      setChecking(false);
    }
  };

  const publishedHead = fairness.data?.audit_head_hash ?? null;
  const b = result?.browser;
  const allAgree = b && b.ok && (!result.server || (result.server.ok && result.server.head_hash === b.headHash)) && (!publishedHead || publishedHead === b.headHash);

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <header className="space-y-2">
        <Link to={`/events/${encodeURIComponent(id)}/fairness`} className="text-sm font-semibold text-ink-2 hover:text-ink">
          ← How this draw stays fair
        </Link>
        <h1 className="font-display text-3xl sm:text-4xl">Audit log</h1>
        <p className="max-w-prose text-ink-2">
          Every important action, in order. Each record includes the hash of the one before it, so changing or removing any record breaks every link after it.
        </p>
      </header>

      <section className="space-y-4 rounded-lg border-2 border-ink bg-paper-2 p-5" aria-labelledby="chain-h">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 id="chain-h" className="flex items-center gap-2 font-display text-xl">
              <Link2 className="size-5" aria-hidden="true" /> Check the chain
            </h2>
            <p className="mt-1 text-sm text-ink-2">Your browser recomputes every hash and compares the result with what the server claims.</p>
          </div>
          <Button onClick={() => void check()} loading={checking}>
            Check the chain in my browser
          </Button>
        </div>

        {head && (
          <div className="text-sm">
            <p className="mb-1 text-ink-3">
              Head: record #{head.seq}
              {publishedHead && (publishedHead === head.hash ? ' (same as on the fairness page)' : ' (differs from the fairness page)')}
            </p>
            <HashValue value={head.hash} label="audit head hash" />
          </div>
        )}

        {checkError && (
          <Alert tone="error" title="The check couldn’t finish">
            {checkError}
          </Alert>
        )}

        {b && allAgree && (
          <Alert tone="success" title="Chain intact">
            {b.checked} records link correctly. Your browser’s head (#{b.headSeq}, {short(b.headHash)}) matches the server
            {publishedHead ? ' and the fairness page' : ''}.
          </Alert>
        )}
        {b && !b.ok && (
          <Alert tone="error" title={`Record #${b.firstBadSeq} breaks the chain`}>
            The records served don’t add up from that point on.
            {result.server?.ok ? ' The server says the chain is fine, but the records it served say otherwise, so don’t take its word for it.' : ''}
          </Alert>
        )}
        {b && b.ok && !allAgree && (
          <Alert tone="error" title="The heads don’t match">
            The records link correctly, but their head ({short(b.headHash)}) differs from what the server
            {result.server ? ` reports (${short(result.server.head_hash)})` : ''}
            {publishedHead && publishedHead !== b.headHash ? ` or the fairness page shows (${short(publishedHead)})` : ''}. Records may be missing.
          </Alert>
        )}
        {result?.serverError && <p className="text-sm text-ink-3">The server’s own verification couldn’t be fetched ({result.serverError}).</p>}
      </section>

      {pages.isPending && <Skeleton className="h-64" />}
      {pages.error && (
        <Alert tone={describeError(pages.error).tone} title={describeError(pages.error).title}>
          {describeError(pages.error).body}
        </Alert>
      )}
      {pages.data && records.length === 0 && <Alert title="Nothing recorded yet">Records appear as the event is set up and run.</Alert>}

      <ol className="space-y-3" aria-label="Audit records">
        {records.map((r) => (
          <RecordCard key={r.seq} r={r} isHead={!!head && head.seq === r.seq} check={checks.get(r.seq)} />
        ))}
      </ol>

      {pages.hasNextPage && (
        <Button variant="secondary" onClick={() => void pages.fetchNextPage()} loading={pages.isFetchingNextPage}>
          Load more
        </Button>
      )}
    </div>
  );
}
