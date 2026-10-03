import { ComingSoon } from '@/pages/ComingSoon';

export function SimPanel() {
  return (
    <ComingSoon title="Simulator" stage={6}>
      Pick an attack scenario, tune it, run it and watch progress live.
    </ComingSoon>
  );
}

export function RunResults() {
  return (
    <ComingSoon title="Run results" stage={6}>
      Fairness, system, detection and integrity numbers, always with their confidence intervals.
    </ComingSoon>
  );
}

export function Compare() {
  return (
    <ComingSoon title="First come, first served vs Fair Drop" stage={6}>
      The same attack against both modes, side by side.
    </ComingSoon>
  );
}

export function Experiments() {
  return (
    <ComingSoon title="Experiments" stage={6}>
      Charts with confidence bands, from the experiments the simulator has run.
    </ComingSoon>
  );
}
