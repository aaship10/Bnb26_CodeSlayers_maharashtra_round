import { IV, compress, loadBlock, sha256, toHex } from '@/features/challenge/sha256';

/**
 * SHA-256 and HMAC-SHA256 for the verifier. WebCrypto when the page is a secure
 * context (it's fast and native); otherwise a pure-JS implementation, because
 * crypto.subtle doesn't exist on http://<lan-ip>. Both are tested against the
 * same vectors, and the verifier cross-checks them on the first entrant.
 */
export interface DrawCrypto {
  name: 'webcrypto' | 'pure-js';
  sha256(data: Uint8Array): Promise<Uint8Array>;
  /** Returns a signer bound to one key (the final seed). */
  hmacSigner(key: Uint8Array): Promise<(msg: Uint8Array) => Promise<Uint8Array>>;
}

/** SHA-256 continuing from a midstate that has already absorbed `prefixLen` bytes (a multiple of 64). */
function sha256From(mid: Int32Array, prefixLen: number, msg: Uint8Array): Uint8Array {
  const h = new Int32Array(mid);
  const w = new Int32Array(64);
  const total = Math.ceil((msg.length + 9) / 64) * 64;
  const buf = new Uint8Array(total);
  buf.set(msg);
  buf[msg.length] = 0x80;
  const bits = (prefixLen + msg.length) * 8;
  const view = new DataView(buf.buffer);
  view.setUint32(total - 8, Math.floor(bits / 0x100000000));
  view.setUint32(total - 4, bits >>> 0);
  for (let off = 0; off < total; off += 64) {
    loadBlock(buf, off, w);
    compress(h, w);
  }
  const out = new Uint8Array(32);
  const ov = new DataView(out.buffer);
  for (let i = 0; i < 8; i++) ov.setInt32(i * 4, h[i]!);
  return out;
}

/**
 * Pure-JS HMAC-SHA256 with the key schedule precomputed: the inner and outer
 * pads are absorbed once, so each message costs two short hashes. That makes
 * 50,000 entrants a few tens of milliseconds.
 */
export function pureHmacFactory(key: Uint8Array): (msg: Uint8Array) => Uint8Array {
  let k = key.length > 64 ? sha256(key) : key;
  const block = new Uint8Array(64);
  block.set(k);
  k = block;
  const ipad = k.map((b) => b ^ 0x36);
  const opad = k.map((b) => b ^ 0x5c);
  const w = new Int32Array(64);
  const inner = new Int32Array(IV);
  loadBlock(ipad, 0, w);
  compress(inner, w);
  const outer = new Int32Array(IV);
  loadBlock(opad, 0, w);
  compress(outer, w);
  return (msg) => sha256From(outer, 64, sha256From(inner, 64, msg));
}

export const pureJsCrypto: DrawCrypto = {
  name: 'pure-js',
  sha256: async (data) => sha256(data),
  hmacSigner: async (key) => {
    const sign = pureHmacFactory(key);
    return async (msg) => sign(msg);
  },
};

function subtle(): SubtleCrypto | null {
  try {
    return typeof crypto !== 'undefined' && crypto.subtle && typeof crypto.subtle.digest === 'function' ? crypto.subtle : null;
  } catch {
    return null;
  }
}

export function webCrypto(s: SubtleCrypto): DrawCrypto {
  return {
    name: 'webcrypto',
    sha256: async (data) => new Uint8Array(await s.digest('SHA-256', data.slice())),
    hmacSigner: async (key) => {
      const k = await s.importKey('raw', key.slice(), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
      return async (msg) => new Uint8Array(await s.sign('HMAC', k, msg.slice()));
    },
  };
}

/** WebCrypto when available, otherwise pure JS. */
export function bestCrypto(): DrawCrypto {
  const s = subtle();
  return s ? webCrypto(s) : pureJsCrypto;
}

export function fromHex(hex: string): Uint8Array {
  if (!/^(?:[0-9a-f]{2})*$/.test(hex)) throw new Error('not lowercase hex');
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

export { toHex };
