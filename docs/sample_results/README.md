# Sample results (SYNTHETIC)

> **Every number in this folder is fake.** The numbers are generated from simple formulas plus seeded noise so the dashboard has realistic shapes to render. Every file says `synthetic: true` and `target: "mock"`, and every run id starts with `synthetic-`. Do not quote these in slides or findings.

Regenerate (deterministic):

```bash
cd simulator && .venv/Scripts/fdsim samples       # Windows
cd simulator && .venv/bin/fdsim samples           # macOS/Linux
```

| Path | What | Mirrors |
|---|---|---|
| `results/synthetic-demo-*.json` | Results JSON (schema_version 1), one per demo preset, 30 runs each with `per_run` | `GET /sim/runs/{id}/results` |
| `charts/<chart_id>.json` | Chart datasets for all six chart ids | `GET /sim/charts/{chart_id}?experiment=` |
| `experiments.json` | Experiment index | `GET /sim/experiments` |
| `schema/*.schema.json` | JSON Schemas generated from the pydantic models | Contract for D's zod schemas |

The demo presets these stand in for:

| File | Story |
|---|---|
| `synthetic-demo-fcfs-flood` | FCFS, 20 speed-bot identities: every bot identity gets a seat (20% of 100 seats) |
| `synthetic-demo-lottery-flood` | Same flood against the lottery: bot share stays at the neutral baseline (~1%) |
| `synthetic-demo-sybil200-off` | 200 Sybil identities on one IP, defences off: share ≈ entrant share (~9%) |
| `synthetic-demo-sybil200-on` | Same attack with all defences on: risk down-weighting brings the share to ~2%, and detection metrics are present |
