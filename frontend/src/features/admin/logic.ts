import type { Phase } from '@/api/schemas';
import { LAYER_IDS, type DefenceConfig, type LayerId, type LifecycleAction, type Preset, type PresetId } from './schemas';

/* --------------------------------------------------------------- lifecycle */

/** Which phase each action needs. Mirrors the server, which is still the authority (409 otherwise). */
export const ACTION_FROM: Record<LifecycleAction, Phase> = {
  schedule: 'DRAFT',
  open: 'SCHEDULED',
  close: 'OPEN',
  draw: 'DRAWING',
};

export function actionEnabled(action: LifecycleAction, phase: Phase): boolean {
  return ACTION_FROM[action] === phase;
}

/** The one action that moves this event forward, if any. */
export function nextAction(phase: Phase): LifecycleAction | null {
  return (Object.keys(ACTION_FROM) as LifecycleAction[]).find((a) => ACTION_FROM[a] === phase) ?? null;
}

/* ---------------------------------------------------------------- defences */

const sameParams = (a: Record<string, unknown>, b: Record<string, unknown>) => JSON.stringify(a) === JSON.stringify(b);

/** If the layers exactly equal a preset's, that preset's id; otherwise "custom". */
export function matchPreset(layers: DefenceConfig['layers'], presets: Preset[]): PresetId {
  for (const p of presets) {
    if (LAYER_IDS.every((id) => sameParams(layers[id], p.defences.layers[id]))) return p.id;
  }
  return 'custom';
}

export function toggleLayer(config: DefenceConfig, id: LayerId, enabled: boolean, presets: Preset[]): DefenceConfig {
  const layers = { ...config.layers, [id]: { ...config.layers[id], enabled } };
  return { preset: matchPreset(layers, presets), layers };
}

/** "per_ip_rps 5 · burst 10" for a layer's parameters (everything except enabled). */
export function paramsSummary(layer: Record<string, unknown>): string {
  return Object.entries(layer)
    .filter(([k]) => k !== 'enabled')
    .map(([k, v]) => `${k} ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join(' · ');
}

export function configsEqual(a: DefenceConfig, b: DefenceConfig): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/* ----------------------------------------------------------------- polling */

export { pollInterval } from '@/api/polling';

/* ---------------------------------------------------------------- forms */

/** ms -> "YYYY-MM-DDTHH:mm" in the viewer's local time, for <input type="datetime-local">. */
export function toLocalInput(ms: number): string {
  const d = new Date(ms);
  const off = d.getTimezoneOffset() * 60_000;
  return new Date(ms - off).toISOString().slice(0, 16);
}

/** "YYYY-MM-DDTHH:mm" in local time -> ISO UTC with Z, or null if empty/invalid. */
export function fromLocalInput(value: string): string | null {
  if (!value) return null;
  const ms = new Date(value).getTime();
  return Number.isFinite(ms) ? new Date(ms).toISOString() : null;
}
