import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Braces } from 'lucide-react';
import { describeError } from '@/api/errors';
import { cx } from '@/lib/cx';
import { uuidv4 } from '@/lib/ids';
import { Alert } from '@/ui/Alert';
import { Button } from '@/ui/Button';
import { Skeleton } from '@/ui/Skeleton';
import { Switch } from '@/ui/Switch';
import { adminApi, adminKeys } from './adminApi';
import { configsEqual, matchPreset, paramsSummary, toggleLayer } from './logic';
import { LAYER_IDS, defenceConfigSchema, type AdminEvent, type DefenceConfig, type LayerId } from './schemas';

const LAYERS: Record<LayerId, { name: string; what: string }> = {
  rate_limit: { name: 'Rate limit', what: 'Caps requests per IP and per account. Answers 429 with Retry-After.' },
  pow: { name: 'Proof-of-work', what: 'Each request costs a little CPU: nothing for one person, a lot at bot scale.' },
  captcha: { name: 'CAPTCHA', what: 'Asks risky clients to complete a human check.' },
  signals: { name: 'Signals', what: 'Device id, honeypot and timing signals feed the risk score.' },
  risk: { name: 'Risk scoring', what: 'Scores each request; challenges or rejects above thresholds.' },
};

/** Parse and validate the raw JSON editor's text. Returns the config or a list of readable problems. */
export function parseDefenceJson(text: string): { ok: true; value: DefenceConfig } | { ok: false; problems: string[] } {
  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch (e) {
    return { ok: false, problems: [`Not valid JSON: ${e instanceof Error ? e.message : String(e)}`] };
  }
  const r = defenceConfigSchema.safeParse(json);
  if (!r.success) return { ok: false, problems: r.error.issues.map((i) => `${i.path.join('.') || '(root)'}: ${i.message}`) };
  return { ok: true, value: r.data };
}

/**
 * Defences for one event: pick a preset, flip individual layers (which turns the
 * preset into "custom"), or edit the raw JSON. Nothing changes on the server
 * until Apply, which PATCHes the whole config with an Idempotency-Key.
 */
export function DefencePanel({ event }: { event: AdminEvent }) {
  const queryClient = useQueryClient();
  const presets = useQuery({ queryKey: adminKeys.presets, queryFn: ({ signal }) => adminApi.presets(signal), staleTime: 5 * 60_000 });
  const saved = event.config.defences;
  const [draft, setDraft] = useState<DefenceConfig>(saved);
  const [jsonOpen, setJsonOpen] = useState(false);
  const [jsonText, setJsonText] = useState('');
  const [jsonProblems, setJsonProblems] = useState<string[]>([]);
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<unknown>(null);
  const [applied, setApplied] = useState(false);

  // When the server copy changes (applied here or by another organizer): follow it only if
  // there are no local edits. Unsaved edits are never silently thrown away.
  const savedJson = JSON.stringify(saved);
  const prevSaved = useRef(savedJson);
  useEffect(() => {
    const before = prevSaved.current;
    prevSaved.current = savedJson;
    setDraft((d) => (JSON.stringify(d) === before ? (JSON.parse(savedJson) as DefenceConfig) : d));
  }, [savedJson]);

  const dirty = !configsEqual(draft, saved);
  const list = presets.data ?? [];
  const shownPreset = useMemo(() => (list.length ? matchPreset(draft.layers, list) : draft.preset), [draft, list]);

  const openJson = () => {
    setJsonText(JSON.stringify(draft, null, 2));
    setJsonProblems([]);
    setJsonOpen(true);
  };

  const useJson = () => {
    const r = parseDefenceJson(jsonText);
    if (!r.ok) return setJsonProblems(r.problems);
    setJsonProblems([]);
    setDraft(r.value);
    setJsonOpen(false);
  };

  const apply = async () => {
    setApplying(true);
    setApplyError(null);
    setApplied(false);
    try {
      const updated = await adminApi.patchConfig(event.id, draft, uuidv4());
      queryClient.setQueryData(adminKeys.event(event.id), updated);
      setDraft(updated.config.defences);
      setApplied(true);
    } catch (e) {
      setApplyError(e);
    } finally {
      setApplying(false);
    }
  };

  const err = applyError ? describeError(applyError) : null;

  return (
    <section aria-labelledby="def-h" className="space-y-4 rounded-lg border-2 border-ink bg-paper-2 p-4 sm:p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="def-h" className="font-display text-xl">
          Defences
        </h2>
        <span className="font-mono text-xs text-ink-3">preset: {shownPreset}</span>
      </div>

      {presets.isPending ? (
        <Skeleton className="h-12" />
      ) : (
        <div role="radiogroup" aria-label="Preset" className="flex flex-wrap gap-2">
          {list.map((p) => (
            <button
              key={p.id}
              type="button"
              role="radio"
              aria-checked={shownPreset === p.id}
              title={p.description}
              onClick={() => setDraft(structuredClone(p.defences))}
              className={cx(
                'rounded-full border-2 border-ink px-3 py-1 text-sm font-semibold',
                shownPreset === p.id ? 'bg-ink text-paper' : 'bg-paper hover:bg-sun-tint',
              )}
            >
              {p.name}
            </button>
          ))}
          <span
            role="radio"
            aria-checked={shownPreset === 'custom'}
            aria-disabled="true"
            className={cx(
              'rounded-full border-2 border-dashed px-3 py-1 text-sm font-semibold',
              shownPreset === 'custom' ? 'border-ink bg-sun text-ink' : 'border-rule text-ink-3',
            )}
          >
            Custom
          </span>
        </div>
      )}

      <ul className="divide-y divide-rule rounded-md border-2 border-ink bg-paper">
        {LAYER_IDS.map((id) => {
          const layer = draft.layers[id];
          const params = paramsSummary(layer);
          return (
            <li key={id} className="flex items-start gap-3 px-3 py-3">
              <Switch
                checked={layer.enabled}
                label={LAYERS[id].name}
                onChange={(on) => setDraft((d) => (list.length ? toggleLayer(d, id, on, list) : { ...d, layers: { ...d.layers, [id]: { ...d.layers[id], enabled: on } } }))}
              />
              <div className="min-w-0">
                <p className="font-semibold">{LAYERS[id].name}</p>
                <p className="text-sm text-ink-2">{LAYERS[id].what}</p>
                {params && <p className="mt-0.5 break-words font-mono text-xs text-ink-3">{params}</p>}
              </div>
            </li>
          );
        })}
      </ul>

      {jsonOpen ? (
        <div className="space-y-2">
          <label htmlFor="def-json" className="block font-display text-sm font-bold">
            Raw config (JSON)
          </label>
          <textarea
            id="def-json"
            value={jsonText}
            onChange={(e) => setJsonText(e.target.value)}
            spellCheck={false}
            rows={14}
            aria-invalid={jsonProblems.length > 0 || undefined}
            aria-describedby="def-json-help"
            className="w-full rounded-md border-2 border-ink bg-paper p-3 font-mono text-xs leading-relaxed"
          />
          <p id="def-json-help" className="text-xs text-ink-3">
            Checked against a provisional schema until B publishes docs/CONFIG_SCHEMA.md. Unknown layer parameters pass through.
          </p>
          {jsonProblems.length > 0 && (
            <Alert tone="warn" title="That config isn’t valid">
              <ul className="list-disc pl-5 font-mono text-xs">
                {jsonProblems.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            </Alert>
          )}
          <div className="flex gap-2">
            <Button size="sm" variant="secondary" onClick={useJson}>
              Use this JSON
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setJsonOpen(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <Button size="sm" variant="ghost" onClick={openJson} leading={<Braces className="size-4" aria-hidden="true" />}>
          Edit as JSON
        </Button>
      )}

      {err && (
        <Alert tone={err.tone} title={err.title}>
          {err.detail ?? err.body}
        </Alert>
      )}
      {applied && !dirty && (
        <Alert tone="success" title="Defences updated">
          The new configuration is live for this event.
        </Alert>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3 border-t-2 border-dashed border-rule pt-4">
        <span className={cx('text-sm', dirty ? 'font-semibold text-ink' : 'text-ink-3')} aria-live="polite">
          {dirty ? 'Unsaved changes' : 'Matches what’s live'}
        </span>
        <div className="flex gap-2">
          <Button size="sm" variant="ghost" disabled={!dirty || applying} onClick={() => setDraft(saved)}>
            Revert
          </Button>
          <Button size="sm" disabled={!dirty} loading={applying} onClick={() => void apply()}>
            Apply
          </Button>
        </div>
      </div>
    </section>
  );
}
