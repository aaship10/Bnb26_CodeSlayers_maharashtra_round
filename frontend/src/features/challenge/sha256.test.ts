import { createHash, randomBytes } from 'node:crypto';
import { sha256, sha256Hex, toHex, utf8 } from './sha256';

describe('sha256 (pure JS)', () => {
  // FIPS 180-4 / NIST example vectors
  it.each([
    ['', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'],
    ['abc', 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'],
    ['abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq', '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1'],
    [
      'abcdefghbcdefghicdefghijdefghijkefghijklfghijklmghijklmnhijklmnoijklmnopjklmnopqklmnopqrlmnopqrsmnopqrstnopqrstu',
      'cf5b16a778af8380036ce59e7b0492370b249b11e8f07a51afac45037afee9d1',
    ],
  ])('NIST vector %j', (input, expected) => {
    expect(sha256Hex(input)).toBe(expected);
  });

  it('one million "a"', () => {
    expect(toHex(sha256(new Uint8Array(1_000_000).fill(0x61)))).toBe('cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0');
  });

  it('matches Node crypto for every length around the padding boundaries', () => {
    for (let len = 0; len <= 200; len++) {
      const data = randomBytes(len);
      expect(toHex(sha256(data)), `len ${len}`).toBe(createHash('sha256').update(data).digest('hex'));
    }
  });

  it('handles non-ASCII input as UTF-8', () => {
    const s = 'zażółć gęślą jaźń 🎟️';
    expect(toHex(sha256(utf8(s)))).toBe(createHash('sha256').update(s, 'utf8').digest('hex'));
  });
});
