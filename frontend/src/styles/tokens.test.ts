import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

/**
 * Guards the palette. Parses tokens.css (the single source of truth) and checks
 * WCAG 2.x contrast for every text/background pairing the UI actually uses.
 */
const css = readFileSync(fileURLToPath(new URL('./tokens.css', import.meta.url)), 'utf8');
const colors: Record<string, string> = {};
for (const m of css.matchAll(/--color-([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;/g)) colors[m[1]!] = m[2]!;

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
}

function contrast(fg: string, bg: string): number {
  const a = luminance(colors[fg]!);
  const b = luminance(colors[bg]!);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

// [text, background, minimum ratio]  (4.5 = AA body text, 3 = AA large text / UI components)
const PAIRS: [string, string, number][] = [
  ['ink', 'paper', 7],
  ['ink', 'paper-2', 7],
  ['ink', 'paper-3', 7],
  ['ink-2', 'paper', 7],
  ['ink-2', 'paper-2', 7],
  ['ink-3', 'paper', 4.5],
  ['ink-3', 'paper-2', 4.5],
  ['ink-3', 'paper-3', 4.5],
  ['white', 'tomato', 4.5],
  ['white', 'tomato-deep', 4.5],
  ['tomato-deep', 'paper', 4.5],
  ['tomato-deep', 'paper-2', 4.5],
  ['tomato-deep', 'tomato-tint', 4.5],
  ['cobalt', 'paper', 4.5],
  ['cobalt', 'paper-2', 4.5],
  ['cobalt', 'cobalt-tint', 4.5],
  ['white', 'cobalt', 4.5],
  ['ink', 'sun', 7],
  ['ink', 'sun-tint', 7],
  ['ink', 'mint', 7],
  ['ink', 'mint-tint', 7],
  ['ink', 'tomato-tint', 7],
  ['ink', 'cobalt-tint', 7],
  ['pine', 'paper', 4.5],
  ['pine', 'mint-tint', 4.5],
  ['white', 'pine', 4.5],
  ['paper', 'ink', 7],
  ['white', 'ink-3', 4.5],
  // non-text: outlines against the page must be visible
  ['ink', 'paper', 3],
];

describe('design tokens', () => {
  it('parsed every colour token', () => {
    for (const name of ['paper', 'ink', 'tomato', 'cobalt', 'sun', 'mint', 'pine', 'white']) expect(colors[name], name).toBeDefined();
  });

  it.each(PAIRS)('%s on %s meets %s:1', (fg, bg, min) => {
    expect(contrast(fg, bg)).toBeGreaterThanOrEqual(min);
  });
});
