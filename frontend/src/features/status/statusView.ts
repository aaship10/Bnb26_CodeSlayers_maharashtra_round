import type { StatusResponse } from '@/api/schemas';

export type StatusViewKind =
  | 'not_open' // window hasn't opened
  | 'not_entered' // window open, no entry yet
  | 'waiting' // entered, waiting for the draw (no rank exists)
  | 'drawing' // draw running
  | 'won' // picked; hold running
  | 'waitlisted'
  | 'lost'
  | 'claimed'
  | 'expired' // hold ran out
  | 'missed'; // window closed without an entry

/**
 * Which story the status page tells. Driven only by the server's state and phase.
 * There is deliberately no way to express "rank" or "your odds" before the draw:
 * waiting and drawing look the same for every entrant.
 */
export function deriveStatusView(s: Pick<StatusResponse, 'state' | 'phase'>): StatusViewKind {
  switch (s.state) {
    case 'CLAIMED':
      return 'claimed';
    case 'EXPIRED':
      return 'expired';
    case 'LOST':
      return 'lost';
    case 'WON':
      return 'won';
    case 'WAITLISTED':
      return 'waitlisted';
    case 'ENTERED':
      // After close the result may take a moment to land; until it does, it is still "drawing".
      return s.phase === 'DRAFT' || s.phase === 'SCHEDULED' || s.phase === 'OPEN' ? 'waiting' : 'drawing';
    case 'REGISTERED':
      if (s.phase === 'DRAFT' || s.phase === 'SCHEDULED') return 'not_open';
      if (s.phase === 'OPEN') return 'not_entered';
      return 'missed';
  }
}

/** What to say to a screen-reader user when the state changes under them. */
export const VIEW_ANNOUNCEMENT: Record<StatusViewKind, string> = {
  not_open: 'The entry window has not opened yet.',
  not_entered: 'The entry window is open. You have not entered yet.',
  waiting: 'You are in the draw. Waiting for the window to close.',
  drawing: 'The draw is running.',
  won: 'You have been picked. Claim your seat before the hold runs out.',
  waitlisted: 'You are on the waitlist.',
  lost: 'The draw did not pick you this time.',
  claimed: 'Your seat is confirmed.',
  expired: 'Your hold ran out.',
  missed: 'Entries closed before you entered.',
};
