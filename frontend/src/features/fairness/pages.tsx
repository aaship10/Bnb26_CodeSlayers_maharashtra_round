import { ComingSoon } from '@/pages/ComingSoon';

export function FairnessPage() {
  return (
    <ComingSoon title="Verify the draw yourself" stage={5}>
      The published commitment, beacon and seed, plus a five-step check that runs entirely in your browser.
    </ComingSoon>
  );
}

export function AuditPage() {
  return (
    <ComingSoon title="Audit log" stage={5}>
      Every recorded action in order, with the hash chain recomputed in your browser.
    </ComingSoon>
  );
}
