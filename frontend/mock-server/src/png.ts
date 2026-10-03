import { deflateSync } from 'node:zlib';

/**
 * Minimal PNG renderer for the mock's /sim/charts/{id}.png. No text (no font
 * rasteriser), just axes, CI bands and series, which is enough to exercise the
 * UI's static-image fallback. C's real service renders proper figures.
 */
const W = 720;
const H = 360;
const PAD = { l: 40, r: 20, t: 20, b: 36 };
const COLORS: [number, number, number][] = [
  [0x24, 0x41, 0xd6],
  [0xeb, 0x68, 0x34],
  [0x1b, 0xaf, 0x7a],
  [0xc9, 0x8a, 0x00],
  [0xe8, 0x7b, 0xa4],
];

type Pt = { x: number | string; y: number; ci_low?: number; ci_high?: number };

export function renderChartPng(series: { name: string; points: Pt[] }[]): Buffer {
  const px = Buffer.alloc(W * H * 3, 0xff);
  const set = (x: number, y: number, c: [number, number, number], a = 1) => {
    if (x < 0 || y < 0 || x >= W || y >= H) return;
    const i = (Math.round(y) * W + Math.round(x)) * 3;
    for (let k = 0; k < 3; k++) px[i + k] = Math.round(px[i + k]! * (1 - a) + c[k]! * a);
  };
  const line = (x0: number, y0: number, x1: number, y1: number, c: [number, number, number], w = 2) => {
    const steps = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0), 1);
    for (let s = 0; s <= steps; s++) {
      const x = x0 + ((x1 - x0) * s) / steps;
      const y = y0 + ((y1 - y0) * s) / steps;
      for (let dx = -w / 2; dx < w / 2; dx++) for (let dy = -w / 2; dy < w / 2; dy++) set(x + dx, y + dy, c);
    }
  };

  const xs = series[0]?.points.map((p) => p.x) ?? [];
  const numeric = xs.every((x) => typeof x === 'number' && x > 0);
  const ymax = Math.max(1e-9, ...series.flatMap((s) => s.points.map((p) => p.ci_high ?? p.y)));
  const sx = (i: number) => PAD.l + (i * (W - PAD.l - PAD.r)) / Math.max(1, xs.length - 1);
  const sy = (v: number) => H - PAD.b - (v / ymax) * (H - PAD.t - PAD.b);
  const grey: [number, number, number] = [0x9a, 0x94, 0x88];
  line(PAD.l, H - PAD.b, W - PAD.r, H - PAD.b, grey, 1);
  line(PAD.l, PAD.t, PAD.l, H - PAD.b, grey, 1);

  series.forEach((s, si) => {
    const c = COLORS[si % COLORS.length]!;
    s.points.forEach((p, i) => {
      if (p.ci_low !== undefined && p.ci_high !== undefined) {
        for (let y = sy(p.ci_high); y <= sy(p.ci_low); y++) for (let x = sx(i) - 6; x <= sx(i) + 6; x++) set(x, y, c, 0.18);
      }
    });
    if (numeric) {
      for (let i = 1; i < s.points.length; i++) line(sx(i - 1), sy(s.points[i - 1]!.y), sx(i), sy(s.points[i]!.y), c);
    }
    s.points.forEach((p, i) => {
      for (let dx = -4; dx <= 4; dx++) for (let dy = -4; dy <= 4; dy++) if (dx * dx + dy * dy <= 16) set(sx(i) + dx + si * (numeric ? 0 : 10), sy(p.y) + dy, c);
    });
  });

  // PNG: signature, IHDR, IDAT (filter byte 0 per row), IEND
  const raw = Buffer.alloc((W * 3 + 1) * H);
  for (let y = 0; y < H; y++) px.copy(raw, y * (W * 3 + 1) + 1, y * W * 3, (y + 1) * W * 3);
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(W, 0);
  ihdr.writeUInt32BE(H, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 2; // RGB
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', ihdr), chunk('IDAT', deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]);
}

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});

function crc32(buf: Buffer): number {
  let c = 0xffffffff;
  for (const b of buf) c = CRC_TABLE[(c ^ b) & 0xff]! ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type: string, data: Buffer): Buffer {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const td = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(td));
  return Buffer.concat([len, td, crc]);
}
