import { EVENTS, PRIMARY_EVENT_ID, timeline } from './events';
import type { World } from './world';

/**
 * A scenario is a named starting point for a demo or test: where the clock is,
 * what the signed-in person has already done, how the draw went, and which
 * failures are armed. Applied on top of a fresh world.
 */
export interface Scenario {
  id: string;
  name: string;
  description: string;
  group: 'Timeline' | 'Outcomes' | 'Failures';
  apply(w: World): void;
}

const primary = EVENTS.find((e) => e.id === PRIMARY_EVENT_ID)!;
const T = timeline(primary);
const MIN = 60_000;
const SEC = 1_000;

/** Named moments in the primary event's life, also offered as time-travel presets. */
export const TIME_PRESETS: { id: string; label: string; at: number }[] = [
  { id: 'before-open', label: '15 min before the window', at: T.opens - 15 * MIN },
  { id: 'just-before-open', label: '10 s before the window', at: T.opens - 10 * SEC },
  { id: 'open', label: 'Window open (+2 min)', at: T.opens + 2 * MIN },
  { id: 'closing', label: 'Last minute of the window', at: T.closes - MIN },
  { id: 'drawing', label: 'Drawing (+2 s after close)', at: T.closes + 2 * SEC },
  { id: 'claiming', label: 'Claiming (hold running)', at: T.drawEnds + 5 * SEC },
  { id: 'hold-ending', label: 'Hold about to run out', at: T.holdEnds - 40 * SEC },
  { id: 'closed', label: 'Everything over', at: T.holdEnds + 10 * SEC },
];

const at = (w: World, ms: number) => w.clock.set(ms);

export const SCENARIOS: Scenario[] = [
  {
    id: 'fresh',
    name: 'Before the window',
    description: 'Countdown to the window opening; nothing entered.',
    group: 'Timeline',
    apply: (w) => at(w, T.opens - 15 * MIN),
  },
  {
    id: 'draft',
    name: 'Draft (not announced)',
    description: 'The event exists but has not been scheduled.',
    group: 'Timeline',
    apply: (w) => {
      at(w, T.opens - 15 * MIN);
      w.setManualPhase(PRIMARY_EVENT_ID, 'DRAFT');
    },
  },
  {
    id: 'window-open',
    name: 'Window open',
    description: 'Entries are open and you have not entered.',
    group: 'Timeline',
    apply: (w) => at(w, T.opens + 2 * MIN),
  },
  {
    id: 'closing-soon',
    name: 'Window closing in 60 s',
    description: 'Not entered yet; watch the window close and the draw start.',
    group: 'Timeline',
    apply: (w) => at(w, T.closes - MIN),
  },
  {
    id: 'entered-waiting',
    name: 'Entered, waiting for the draw',
    description: 'You are in. No rank exists yet.',
    group: 'Timeline',
    apply: (w) => {
      at(w, T.opens + 10 * MIN);
      w.autoEnter = true;
    },
  },
  {
    id: 'drawing',
    name: 'Drawing in progress',
    description: 'The window just closed; the draw is running.',
    group: 'Timeline',
    apply: (w) => {
      at(w, T.closes + 2 * SEC);
      w.autoEnter = true;
    },
  },
  {
    id: 'missed-window',
    name: 'Missed the window',
    description: 'Registered but never entered; the draw is done.',
    group: 'Outcomes',
    apply: (w) => at(w, T.drawEnds + 5 * MIN),
  },
  {
    id: 'won-hold',
    name: 'Won: hold running',
    description: 'You were picked; a 10-minute hold is ticking.',
    group: 'Outcomes',
    apply: (w) => {
      at(w, T.drawEnds + 5 * SEC);
      w.autoEnter = true;
      w.outcome = 'won';
    },
  },
  {
    id: 'won-hold-ending',
    name: 'Won: hold about to run out',
    description: 'Forty seconds left on the hold.',
    group: 'Outcomes',
    apply: (w) => {
      at(w, T.holdEnds - 40 * SEC);
      w.autoEnter = true;
      w.outcome = 'won';
    },
  },
  {
    id: 'hold-expired',
    name: 'Hold expired',
    description: 'You won but did not claim in time.',
    group: 'Outcomes',
    apply: (w) => {
      at(w, T.holdEnds + 10 * SEC);
      w.autoEnter = true;
      w.outcome = 'won';
    },
  },
  {
    id: 'waitlisted',
    name: 'Waitlisted',
    description: 'Not picked; a waitlist position is shown after the draw.',
    group: 'Outcomes',
    apply: (w) => {
      at(w, T.drawEnds + 5 * SEC);
      w.autoEnter = true;
      w.outcome = 'waitlisted';
    },
  },
  {
    id: 'lost',
    name: 'Lost (event closed)',
    description: 'Waitlisted and the claim window has ended.',
    group: 'Outcomes',
    apply: (w) => {
      at(w, T.holdEnds + 10 * SEC);
      w.autoEnter = true;
      w.outcome = 'waitlisted';
    },
  },
  {
    id: 'claimed',
    name: 'Claimed',
    description: 'You already have a ticket.',
    group: 'Outcomes',
    apply: (w) => {
      at(w, T.drawEnds + MIN);
      w.autoEnter = true;
      w.autoClaim = true;
      w.outcome = 'won';
    },
  },
  {
    id: 'enter-rate-limited',
    name: 'Enter: rate limited (429)',
    description: 'Entering returns 429 with Retry-After for about 8 s, then recovers.',
    group: 'Failures',
    apply: (w) => {
      at(w, T.opens + 2 * MIN);
      w.armFault('enter:rate_limited');
    },
  },
  {
    id: 'enter-challenge-pow',
    name: 'Enter: proof-of-work challenge',
    description: 'Entering demands a PoW solution, then succeeds.',
    group: 'Failures',
    apply: (w) => {
      at(w, T.opens + 2 * MIN);
      w.armFault('enter:challenge_pow');
    },
  },
  {
    id: 'enter-challenge-captcha',
    name: 'Enter: CAPTCHA challenge',
    description: 'Entering demands a mock CAPTCHA, then succeeds.',
    group: 'Failures',
    apply: (w) => {
      at(w, T.opens + 2 * MIN);
      w.armFault('enter:challenge_captcha');
    },
  },
  {
    id: 'enter-rejected',
    name: 'Enter: rejected',
    description: 'Entering returns 403 REJECTED until you clear the fault.',
    group: 'Failures',
    apply: (w) => {
      at(w, T.opens + 2 * MIN);
      w.armFault('enter:rejected');
    },
  },
  {
    id: 'flaky-network',
    name: 'Flaky network',
    description: 'The next 3 enter and 3 status calls drop the connection.',
    group: 'Failures',
    apply: (w) => {
      at(w, T.opens + 10 * MIN);
      w.autoEnter = true;
      w.armFault('enter:network_fail');
      w.armFault('status:network_fail');
    },
  },
  {
    id: 'no-sse',
    name: 'SSE unavailable (polling)',
    description: 'Entered; the live stream is off, so status falls back to polling every 5 to 10 s.',
    group: 'Failures',
    apply: (w) => {
      at(w, T.closes - 30 * SEC);
      w.autoEnter = true;
      w.sseEnabled = false;
    },
  },
  {
    id: 'claim-challenge',
    name: 'Claim: PoW challenge',
    description: 'Won, and claiming demands a PoW solution first.',
    group: 'Failures',
    apply: (w) => {
      at(w, T.drawEnds + 5 * SEC);
      w.autoEnter = true;
      w.armFault('claim:challenge_pow');
    },
  },
  {
    id: 'claim-rate-limited',
    name: 'Claim: rate limited (429)',
    description: 'Won, and claiming is rate limited for about 8 s first.',
    group: 'Failures',
    apply: (w) => {
      at(w, T.drawEnds + 5 * SEC);
      w.autoEnter = true;
      w.armFault('claim:rate_limited');
    },
  },
];
