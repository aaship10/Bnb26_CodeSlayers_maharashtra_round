import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { describeError } from '@/api/errors';
import { uuidv4 } from '@/lib/ids';
import { serverClock } from '@/lib/serverClock';
import { Alert } from '@/ui/Alert';
import { Button } from '@/ui/Button';
import { Dialog } from '@/ui/Dialog';
import { Field } from '@/ui/Field';
import { adminApi, adminKeys } from './adminApi';
import { fromLocalInput, toLocalInput } from './logic';
import type { CreateEventBody, PresetId } from './schemas';

const tzName =
  new Intl.DateTimeFormat(undefined, { timeZoneName: 'short' }).formatToParts(new Date()).find((p) => p.type === 'timeZoneName')?.value ?? 'local time';

interface FormState {
  name: string;
  description: string;
  inventory: string;
  opens: string;
  closes: string;
  ttlMin: string;
  mode: 'LOTTERY' | 'FCFS';
  preset: Exclude<PresetId, 'custom'>;
}

function defaults(): FormState {
  // Server time, not this laptop's: a demo machine with a wrong clock must not create a past window.
  const start = Math.ceil((serverClock.now() + 10 * 60_000) / 60_000) * 60_000;
  return {
    name: '',
    description: '',
    inventory: '500',
    opens: toLocalInput(start),
    closes: toLocalInput(start + 30 * 60_000),
    ttlMin: '10',
    mode: 'LOTTERY',
    preset: 'rate_limit+pow',
  };
}

type Errors = Partial<Record<keyof FormState, string>>;

export function validateCreate(f: FormState): Errors {
  const e: Errors = {};
  if (!f.name.trim()) e.name = 'Give the event a name.';
  else if (f.name.trim().length > 120) e.name = 'Keep the name under 120 characters.';
  const inv = Number(f.inventory);
  if (!Number.isInteger(inv) || inv < 1) e.inventory = 'Seats must be a whole number, at least 1.';
  else if (inv > 1_000_000) e.inventory = 'That’s more than 1,000,000 seats.';
  const o = fromLocalInput(f.opens);
  const c = fromLocalInput(f.closes);
  if (!o) e.opens = 'Pick when the window opens.';
  if (!c) e.closes = 'Pick when the window closes.';
  if (o && c && Date.parse(c) <= Date.parse(o)) e.closes = 'The window must close after it opens.';
  const ttl = Number(f.ttlMin);
  if (!Number.isFinite(ttl) || ttl < 1 || ttl > 1440) e.ttlMin = 'Between 1 minute and 24 hours.';
  return e;
}

/** Create an event (lands in Draft). One Idempotency-Key per opened form, reused if submit is retried. */
export function CreateEventDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const presets = useQuery({ queryKey: adminKeys.presets, queryFn: ({ signal }) => adminApi.presets(signal), staleTime: 5 * 60_000, enabled: open });
  const [f, setF] = useState<FormState>(defaults);
  const [errors, setErrors] = useState<Errors>({});
  const [serverError, setServerError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const key = useRef(uuidv4());
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setF(defaults());
      setErrors({});
      setServerError(null);
      key.current = uuidv4();
    }
  }, [open]);

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setF((prev) => ({ ...prev, [k]: v }));
  const chosen = useMemo(() => presets.data?.find((p) => p.id === f.preset), [presets.data, f.preset]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (pending) return;
    const errs = validateCreate(f);
    setErrors(errs);
    if (Object.keys(errs).length > 0 || !chosen) return;
    const body: CreateEventBody = {
      name: f.name.trim(),
      description: f.description.trim() || undefined,
      inventory: Number(f.inventory),
      window_opens_at: fromLocalInput(f.opens)!,
      window_closes_at: fromLocalInput(f.closes)!,
      claim_ttl_s: Math.round(Number(f.ttlMin) * 60),
      mode: f.mode,
      config: { defences: structuredClone(chosen.defences) },
    };
    setPending(true);
    setServerError(null);
    try {
      const created = await adminApi.create(body, key.current);
      queryClient.setQueryData(adminKeys.event(created.id), created);
      void queryClient.invalidateQueries({ queryKey: adminKeys.events });
      onClose();
      navigate(`/admin/events/${encodeURIComponent(created.id)}`);
    } catch (err) {
      setServerError(err);
    } finally {
      setPending(false);
    }
  };

  const se = serverError ? describeError(serverError) : null;

  return (
    <Dialog open={open} title="New event" onClose={pending ? undefined : onClose} initialFocus={nameRef} size="md">
      <form onSubmit={submit} noValidate className="space-y-5" aria-label="New event">
        <Field ref={nameRef} label="Name" value={f.name} onChange={(e) => set('name', e.target.value)} error={errors.name} maxLength={140} />
        <Field label="Description (optional)" value={f.description} onChange={(e) => set('description', e.target.value)} />

        <div className="grid gap-5 sm:grid-cols-2">
          <Field label="Seats" type="number" inputMode="numeric" min={1} value={f.inventory} onChange={(e) => set('inventory', e.target.value)} error={errors.inventory} />
          <Field
            label="Time to claim (minutes)"
            type="number"
            inputMode="numeric"
            min={1}
            value={f.ttlMin}
            onChange={(e) => set('ttlMin', e.target.value)}
            error={errors.ttlMin}
            hint="How long a winner's seat is held."
          />
          <Field label="Window opens" type="datetime-local" value={f.opens} onChange={(e) => set('opens', e.target.value)} error={errors.opens} hint={`Your time (${tzName}). Stored in UTC.`} />
          <Field label="Window closes" type="datetime-local" value={f.closes} onChange={(e) => set('closes', e.target.value)} error={errors.closes} />
        </div>

        <fieldset>
          <legend className="mb-2 font-display text-sm font-bold">Allocation</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            {(
              [
                ['LOTTERY', 'Fair draw', 'Everyone in the window gets one equal entry.'],
                ['FCFS', 'First come, first served', 'Baseline for comparison: speed wins.'],
              ] as const
            ).map(([value, label, hint]) => (
              <label key={value} className="flex cursor-pointer gap-3 rounded-md border-2 border-ink bg-paper p-3 has-[:checked]:bg-sun-tint">
                <input type="radio" name="mode" value={value} checked={f.mode === value} onChange={() => set('mode', value)} className="mt-1 accent-[var(--color-ink)]" />
                <span>
                  <span className="block font-semibold">{label}</span>
                  <span className="block text-sm text-ink-2">{hint}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        <div>
          <label htmlFor="preset" className="mb-1.5 block font-display text-sm font-bold">
            Defences
          </label>
          <select
            id="preset"
            value={f.preset}
            onChange={(e) => set('preset', e.target.value as FormState['preset'])}
            className="min-h-12 w-full rounded-md border-2 border-ink bg-paper-2 px-3"
            disabled={!presets.data}
          >
            {(presets.data ?? []).map((p) =>
              p.id === 'custom' ? null : (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ),
            )}
          </select>
          {chosen && <p className="mt-1.5 text-sm text-ink-3">{chosen.description} You can fine-tune layers after creating it.</p>}
        </div>

        {se && (
          <Alert tone={se.tone} title={se.title}>
            {se.detail ?? se.body}
          </Alert>
        )}

        <div className="flex justify-end gap-3">
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button type="submit" loading={pending} disabled={!chosen}>
            Create as draft
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
