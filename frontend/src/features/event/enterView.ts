import type { Phase, UserState } from '@/api/schemas';

export type EnterViewKind =
  | 'draft'
  | 'not_open_yet'
  | 'sign_in'
  | 'checking'
  | 'can_enter'
  | 'entered'
  | 'view_status'
  | 'closed';

export interface EnterViewInput {
  /** Phase according to the SERVER. */
  phase: Phase;
  signedIn: boolean;
  /** Our own mutation just succeeded. */
  justEntered: boolean;
  statusLoading: boolean;
  statusState: UserState | undefined;
}

/**
 * What the entry card should show. Pure so every combination is unit-tested.
 * Only server-provided phase and state decide this; no clock is consulted.
 *
 * If the status lookup failed (statusState undefined, not loading) on an open
 * window we still offer Enter: it is idempotent, so a person who had already
 * entered just gets "already entered" back.
 */
export function deriveEnterView(i: EnterViewInput): EnterViewKind {
  if (i.justEntered) return 'entered';
  if (i.phase === 'DRAFT') return 'draft';
  if (i.phase === 'SCHEDULED') return 'not_open_yet';

  if (i.phase === 'OPEN') {
    if (!i.signedIn) return 'sign_in';
    if (i.statusLoading) return 'checking';
    if (i.statusState === 'ENTERED') return 'entered';
    if (i.statusState === undefined || i.statusState === 'REGISTERED') return 'can_enter';
    return 'view_status';
  }

  // DRAWING, CLAIMING, CLOSED: entries are over. Signed-in people who took part go to their status.
  if (i.signedIn && i.statusState !== undefined && i.statusState !== 'REGISTERED') return 'view_status';
  return 'closed';
}
