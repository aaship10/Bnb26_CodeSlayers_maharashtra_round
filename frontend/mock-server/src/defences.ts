/**
 * Defence configuration as the shared conventions describe it:
 *   events.config.defences = { preset, layers: { rate_limit, pow, captcha, signals, risk } }
 * each layer { enabled, ...params }. Param names here are the mock's guesses;
 * B's docs/CONFIG_SCHEMA.md is authoritative once it exists.
 */
export const PRESET_IDS = ['none', 'rate_limit', 'rate_limit+pow', 'rate_limit+pow+captcha', 'all', 'custom'] as const;
export type PresetId = (typeof PRESET_IDS)[number];
export const LAYER_IDS = ['rate_limit', 'pow', 'captcha', 'signals', 'risk'] as const;
export type LayerId = (typeof LAYER_IDS)[number];

export interface Layer {
  enabled: boolean;
  [param: string]: unknown;
}

export interface DefenceConfig {
  preset: PresetId;
  layers: Record<LayerId, Layer>;
}

const PARAMS: Record<LayerId, Record<string, unknown>> = {
  rate_limit: { per_ip_rps: 5, per_user_rps: 2, burst: 10 },
  pow: { difficulty_bits: 18 },
  captcha: { provider: 'mock', when_risk_at_least: 0.7 },
  signals: { device_id: true, honeypot: true, timing: true },
  risk: { challenge_at: 0.5, reject_at: 0.9 },
};

function build(preset: PresetId, on: LayerId[]): DefenceConfig {
  const layers = Object.fromEntries(LAYER_IDS.map((id) => [id, { enabled: on.includes(id), ...PARAMS[id] }])) as Record<LayerId, Layer>;
  return { preset, layers };
}

export const PRESETS: { id: Exclude<PresetId, 'custom'>; name: string; description: string; defences: DefenceConfig }[] = [
  { id: 'none', name: 'None', description: 'No defences. Use it to show what an unprotected drop looks like.', defences: build('none', []) },
  { id: 'rate_limit', name: 'Rate limit', description: 'Per-IP and per-account request caps.', defences: build('rate_limit', ['rate_limit']) },
  {
    id: 'rate_limit+pow',
    name: 'Rate limit + proof-of-work',
    description: 'Each entry costs a little CPU; cheap for a person, costly at bot scale.',
    defences: build('rate_limit+pow', ['rate_limit', 'pow']),
  },
  {
    id: 'rate_limit+pow+captcha',
    name: 'Rate limit + PoW + CAPTCHA',
    description: 'Adds a human check for risky clients.',
    defences: build('rate_limit+pow+captcha', ['rate_limit', 'pow', 'captcha']),
  },
  { id: 'all', name: 'All layers', description: 'Everything, including signals and risk scoring.', defences: build('all', [...LAYER_IDS]) },
];

export function presetConfig(id: PresetId): DefenceConfig {
  const p = PRESETS.find((x) => x.id === id);
  return structuredClone((p ?? PRESETS[0]!).defences);
}

export function defaultDefences(eventId: string): DefenceConfig {
  return presetConfig(eventId === 'evt_demo_01' ? 'rate_limit+pow' : 'rate_limit');
}

/** Minimal structural validation (what B's schema will check properly). Returns an error message or null. */
export function validateDefences(input: unknown): string | null {
  if (!input || typeof input !== 'object') return 'defences must be an object';
  const d = input as { preset?: unknown; layers?: unknown };
  if (typeof d.preset !== 'string' || !(PRESET_IDS as readonly string[]).includes(d.preset)) return `unknown preset: ${String(d.preset)}`;
  if (!d.layers || typeof d.layers !== 'object') return 'layers must be an object';
  for (const id of LAYER_IDS) {
    const layer = (d.layers as Record<string, unknown>)[id];
    if (!layer || typeof layer !== 'object') return `layers.${id} is missing`;
    if (typeof (layer as { enabled?: unknown }).enabled !== 'boolean') return `layers.${id}.enabled must be true or false`;
  }
  return null;
}
