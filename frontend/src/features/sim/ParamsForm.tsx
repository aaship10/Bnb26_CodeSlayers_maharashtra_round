import { cx } from '@/lib/cx';
import type { FormField } from './jsonSchemaForm';

interface Props {
  fields: FormField[];
  values: Record<string, string | boolean>;
  errors: Record<string, string>;
  onChange: (key: string, value: string | boolean) => void;
}

const inputCls = 'min-h-11 w-full rounded-md border-2 bg-paper-2 px-3 text-base focus:border-cobalt focus:outline-none';

/** Renders a parameter form from the scenario's JSON Schema (see jsonSchemaForm.ts for the supported subset). */
export function ParamsForm({ fields, values, errors, onChange }: Props) {
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {fields.map((f) => {
        const id = `param-${f.key}`;
        const err = errors[f.key];
        const describedBy = [f.description ? `${id}-d` : '', err ? `${id}-e` : ''].filter(Boolean).join(' ') || undefined;
        const range = f.min !== undefined || f.max !== undefined ? ` (${f.min ?? '…'} to ${f.max ?? '…'})` : '';

        if (f.kind === 'boolean') {
          return (
            <div key={f.key} className="sm:col-span-2">
              <label className="flex items-start gap-3">
                <input
                  id={id}
                  type="checkbox"
                  checked={values[f.key] === true}
                  onChange={(e) => onChange(f.key, e.target.checked)}
                  className="mt-1 size-4 accent-[var(--color-ink)]"
                  aria-describedby={describedBy}
                />
                <span>
                  <span className="font-semibold">{f.label}</span>
                  {f.description && (
                    <span id={`${id}-d`} className="block text-sm text-ink-3">
                      {f.description}
                    </span>
                  )}
                </span>
              </label>
            </div>
          );
        }

        return (
          <div key={f.key}>
            <label htmlFor={id} className="mb-1 block font-display text-sm font-bold">
              {f.label}
            </label>
            {f.kind === 'enum' ? (
              <select
                id={id}
                value={String(values[f.key] ?? '')}
                onChange={(e) => onChange(f.key, e.target.value)}
                className={cx(inputCls, 'border-ink')}
                aria-describedby={describedBy}
                aria-required={f.required || undefined}
              >
                {f.options?.map((o) => (
                  <option key={String(o)} value={String(o)}>
                    {String(o)}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={id}
                type={f.kind === 'integer' || f.kind === 'number' ? 'number' : 'text'}
                inputMode={f.kind === 'integer' ? 'numeric' : f.kind === 'number' ? 'decimal' : undefined}
                min={f.min}
                max={f.max}
                step={f.step}
                value={String(values[f.key] ?? '')}
                onChange={(e) => onChange(f.key, e.target.value)}
                className={cx(inputCls, err ? 'border-tomato-deep' : 'border-ink')}
                aria-invalid={err ? true : undefined}
                aria-required={f.required || undefined}
                aria-describedby={describedBy}
                placeholder={f.kind === 'number-list' ? 'e.g. 1, 10, 100' : undefined}
              />
            )}
            <p id={`${id}-d`} className="mt-1 text-xs text-ink-3">
              {f.description ?? ''}
              {range}
            </p>
            {err && (
              <p id={`${id}-e`} className="mt-1 text-sm font-semibold text-tomato-deep">
                {err}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}
