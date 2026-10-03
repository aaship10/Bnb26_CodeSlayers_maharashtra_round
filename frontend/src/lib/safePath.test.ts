import { safeInternalPath } from './safePath';

describe('safeInternalPath (post-sign-in redirect)', () => {
  it('allows same-origin paths', () => {
    expect(safeInternalPath('/events/evt_1')).toBe('/events/evt_1');
    expect(safeInternalPath('/')).toBe('/');
    expect(safeInternalPath('/events/x?tab=1#top')).toBe('/events/x?tab=1#top');
  });

  it.each(['//evil.com', '/\\evil.com', 'https://evil.com', 'javascript:alert(1)', 'events/x', '', '/ok\nhttps://evil.com'])(
    'rejects %j',
    (v) => {
      expect(safeInternalPath(v)).toBe('/');
    },
  );

  it('rejects non-strings and honours the fallback', () => {
    expect(safeInternalPath(undefined, '/home')).toBe('/home');
    expect(safeInternalPath({ a: 1 })).toBe('/');
    expect(safeInternalPath(42)).toBe('/');
  });
});
