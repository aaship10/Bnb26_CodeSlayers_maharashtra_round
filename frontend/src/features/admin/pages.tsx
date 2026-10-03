import { ComingSoon } from '@/pages/ComingSoon';

export function AdminHome() {
  return (
    <ComingSoon title="Organizer dashboard" stage={4}>
      Create events, run the lifecycle, switch defences and watch live stats. Behind an admin token, never linked from the attendee nav.
    </ComingSoon>
  );
}

export function AdminEvent() {
  return (
    <ComingSoon title="Run an event" stage={4}>
      Phase timeline, defence panel, live stats, the invariants badge and the reset button.
    </ComingSoon>
  );
}
