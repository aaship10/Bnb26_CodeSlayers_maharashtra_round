# Chaos scripts

`chaos.py` runs a short load through the public edge, injects ONE fault, heals it, and checks the invariants (A's checker plus
end-to-end checks against Postgres). It writes `results/<fault>.json` = `{fault, recovery_seconds, requests_failed, invariants_passed, ...}`.

    .\infra\fd.ps1 up -Edge nginx -Sim -Mail file
    .\infra\fd.ps1 chaos all          # or: kill-replica kill-worker restart-redis pause-postgres terminate-pg-connections restart-edge

Measured results, what they do NOT show, and what the runs found: `docs/CHAOS.md`.
