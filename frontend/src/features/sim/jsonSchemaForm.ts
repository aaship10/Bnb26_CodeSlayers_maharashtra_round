import type { JsonSchema } from './schemas';

/**
 * The subset of JSON Schema the parameter form understands. Anything outside it
 * is reported (so C knows to keep schemas simple) rather than silently dropped.
 */
export type FieldKind = 'integer' | 'number' | 'boolean' | 'enum' | 'string' | 'number-list';

export interface FormField {
  key: string;
  kind: FieldKind;
  label: string;
  description?: string;
  required: boolean;
  default: unknown;
  min?: number;
  max?: number;
  step?: number | 'any';
  options?: unknown[];
}

export interface ParsedForm {
  fields: FormField[];
  unsupported: string[];
}

export function parseParamsSchema(schema: JsonSchema): ParsedForm {
  const fields: FormField[] = [];
  const unsupported: string[] = [];
  if (schema.type && schema.type !== 'object') return { fields, unsupported: [`top-level type "${schema.type}" (expected object)`] };
  const required = new Set(schema.required ?? []);
  for (const [key, s] of Object.entries(schema.properties ?? {})) {
    const base = { key, label: s.title ?? key, description: s.description, required: required.has(key), default: s.default };
    if (Array.isArray(s.enum)) fields.push({ ...base, kind: 'enum', options: s.enum });
    else if (s.type === 'integer') fields.push({ ...base, kind: 'integer', min: s.minimum, max: s.maximum, step: s.multipleOf ?? 1 });
    else if (s.type === 'number') fields.push({ ...base, kind: 'number', min: s.minimum, max: s.maximum, step: s.multipleOf ?? 'any' });
    else if (s.type === 'boolean') fields.push({ ...base, kind: 'boolean' });
    else if (s.type === 'string') fields.push({ ...base, kind: 'string' });
    else if (s.type === 'array' && (s.items?.type === 'number' || s.items?.type === 'integer'))
      fields.push({ ...base, kind: 'number-list', min: s.items.minimum, max: s.items.maximum });
    else unsupported.push(`${key} (${s.type ?? 'no type'})`);
  }
  return { fields, unsupported };
}

/** Initial form values: defaults, as the strings/booleans the inputs hold. */
export function initialValues(fields: FormField[]): Record<string, string | boolean> {
  const out: Record<string, string | boolean> = {};
  for (const f of fields) {
    if (f.kind === 'boolean') out[f.key] = f.default === true;
    else if (f.kind === 'number-list') out[f.key] = Array.isArray(f.default) ? f.default.join(', ') : '';
    else out[f.key] = f.default === undefined || f.default === null ? '' : String(f.default);
  }
  return out;
}

export interface Coerced {
  values: Record<string, unknown>;
  errors: Record<string, string>;
}

/** Turn input strings into typed values and check them against the schema's limits. */
export function coerceAndValidate(fields: FormField[], raw: Record<string, string | boolean>): Coerced {
  const values: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  const range = (f: FormField, n: number) => {
    if (f.min !== undefined && n < f.min) return `At least ${f.min}.`;
    if (f.max !== undefined && n > f.max) return `At most ${f.max}.`;
    return null;
  };

  for (const f of fields) {
    const v = raw[f.key];
    if (f.kind === 'boolean') {
      values[f.key] = v === true;
      continue;
    }
    const text = typeof v === 'string' ? v.trim() : '';
    if (text === '') {
      if (f.required) errors[f.key] = 'Required.';
      continue;
    }
    if (f.kind === 'integer' || f.kind === 'number') {
      const n = Number(text);
      if (!Number.isFinite(n)) errors[f.key] = 'Enter a number.';
      else if (f.kind === 'integer' && !Number.isInteger(n)) errors[f.key] = 'Enter a whole number.';
      else {
        const r = range(f, n);
        if (r) errors[f.key] = r;
        else values[f.key] = n;
      }
    } else if (f.kind === 'enum') {
      const match = f.options?.find((o) => String(o) === text);
      if (match === undefined) errors[f.key] = 'Pick one of the options.';
      else values[f.key] = match;
    } else if (f.kind === 'number-list') {
      const parts = text.split(/[\s,]+/).filter(Boolean).map(Number);
      const bad = parts.find((n) => !Number.isFinite(n));
      if (bad !== undefined || parts.length === 0) errors[f.key] = 'Enter numbers separated by commas.';
      else {
        const r = parts.map((n) => range(f, n)).find(Boolean);
        if (r) errors[f.key] = r;
        else values[f.key] = parts;
      }
    } else values[f.key] = text;
  }
  return { values, errors };
}

/** Only what differs from the scenario defaults goes in `overrides`. */
export function overridesFrom(fields: FormField[], values: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of fields) {
    if (!(f.key in values)) continue;
    if (JSON.stringify(values[f.key]) !== JSON.stringify(f.default)) out[f.key] = values[f.key];
  }
  return out;
}
