import { deriveEnterView, type EnterViewInput } from './enterView';

const base: EnterViewInput = { phase: 'OPEN', signedIn: true, justEntered: false, statusLoading: false, statusState: 'REGISTERED' };
const view = (o: Partial<EnterViewInput>) => deriveEnterView({ ...base, ...o });

describe('deriveEnterView', () => {
  it('before the window: never offers Enter, signed in or not', () => {
    expect(view({ phase: 'SCHEDULED' })).toBe('not_open_yet');
    expect(view({ phase: 'SCHEDULED', signedIn: false })).toBe('not_open_yet');
    expect(view({ phase: 'DRAFT' })).toBe('draft');
  });

  it('open window', () => {
    expect(view({ signedIn: false, statusState: undefined })).toBe('sign_in');
    expect(view({ statusLoading: true, statusState: undefined })).toBe('checking');
    expect(view({ statusState: 'REGISTERED' })).toBe('can_enter');
    expect(view({ statusState: 'ENTERED' })).toBe('entered');
  });

  it('a failed status lookup still lets a signed-in person try (entering is idempotent)', () => {
    expect(view({ statusState: undefined })).toBe('can_enter');
  });

  it('just entered wins over everything, even a stale status', () => {
    expect(view({ justEntered: true, statusState: 'REGISTERED' })).toBe('entered');
    expect(view({ justEntered: true, phase: 'CLOSED' })).toBe('entered');
  });

  it('after the window: participants go to status, everyone else sees "closed"', () => {
    for (const phase of ['DRAWING', 'CLAIMING', 'CLOSED'] as const) {
      expect(view({ phase, statusState: 'ENTERED' })).toBe('view_status');
      expect(view({ phase, statusState: 'WON' })).toBe('view_status');
      expect(view({ phase, statusState: 'REGISTERED' })).toBe('closed');
      expect(view({ phase, signedIn: false, statusState: undefined })).toBe('closed');
    }
  });

  it('in an open window, post-draw states (should not happen) route to status rather than offering Enter', () => {
    expect(view({ statusState: 'WON' })).toBe('view_status');
  });

  it('never offers Enter unless the SERVER says the window is open', () => {
    const phases = ['DRAFT', 'SCHEDULED', 'DRAWING', 'CLAIMING', 'CLOSED'] as const;
    for (const phase of phases) {
      for (const statusState of [undefined, 'REGISTERED', 'ENTERED'] as const) {
        expect(view({ phase, statusState })).not.toBe('can_enter');
      }
    }
  });
});
