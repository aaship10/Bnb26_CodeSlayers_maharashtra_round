# Thin wrapper around infra/local/run.py (no Docker).
#   .\infra\fd.ps1 up [-N 3]      .\infra\fd.ps1 down      .\infra\fd.ps1 status
#   .\infra\fd.ps1 scale -N 5     .\infra\fd.ps1 logs      .\infra\fd.ps1 test
#   .\infra\fd.ps1 exec python scripts/seed_identities.py --users 50000
#   .\infra\fd.ps1 get-nginx; .\infra\fd.ps1 up -Edge nginx     (nginx as the edge instead of the Python gateway)
#   .\infra\fd.ps1 kill 8002 | heal | chaos all | edge-check
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("up", "down", "status", "scale", "logs", "test", "exec", "setup", "get-redis", "check", "resilience", "get-nginx", "kill", "heal", "chaos", "edge-check")]
    [string]$Cmd,
    [int]$N = 3,
    [ValidateSet('auto', 'file')]
    [string]$Mail = 'auto',
    [ValidateSet('gateway', 'nginx')]
    [string]$Edge = 'gateway',
    [ValidateSet('stub', 'real')]
    [string]$Backend = 'stub',
    [switch]$Sim,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Run = Join-Path $Root "infra\local\run.py"

if ($Cmd -eq "setup") {
    if (-not (Test-Path $Py)) { python -m venv (Join-Path $Root ".venv") }
    & $Py -m pip install -q -r (Join-Path $Root "backend\tests_defence\requirements.txt") "uvicorn[standard]" PyJWT aiosmtplib alembic sqlalchemy redis "fakeredis[lua]"
    return
}
switch ($Cmd) {
    "up"     { if ($Sim) { & $Py $Run up --replicas $N --mail $Mail --edge $Edge --backend $Backend --sim } else { & $Py $Run up --replicas $N --mail $Mail --edge $Edge --backend $Backend } }
    "down"   { & $Py $Run down }
    "status" { & $Py $Run status }
    "scale"  { & $Py $Run scale $N }
    "logs"   { & $Py $Run logs @Rest }
    "test"   {
        # Integration tests need the private Postgres: start it (idempotent), then run everything.
        # No pass-through flags: PowerShell would bind -p / -q to its own common parameters.
        & $Py (Join-Path $Root "infra\local\pg.py") start
        & $Py -m pytest (Join-Path $Root "backend\tests_defence") -q
    }
    "exec"   { & $Py $Run exec -- @Rest }
    "resilience" {
        # Live resilience suite (simulation mode, file mail): graceful drain with traffic flowing, then a
        # flash crowd with consistency checks. Restarts the stack normally afterwards.
        & $Py $Run down
        & $Py $Run up --replicas 3 --mail file --sim
        & $Py $Run exec -- python scripts/drain_test.py
        & $Py $Run exec -- python scripts/scale_test.py --users 1500 --procs 3 --concurrency 40 --direct
        & $Py $Run down
        & $Py $Run up --replicas 3
    }
    "get-nginx" { & $Py (Join-Path $Root "infra\local\get_nginx.py") }
    "kill"   { & $Py $Run kill @Rest }
    "heal"   { & $Py $Run heal }
    "chaos"  {
        # One fault (kill-replica, kill-worker, restart-redis, pause-postgres, terminate-pg-connections, restart-edge) or `all`.
        # Needs the stack up in simulation mode with file mail: .\infra\fd.ps1 up -Sim -Mail file [-Edge nginx]
        if (-not $Rest) { $Rest = @("all") }
        & $Py $Run exec -- python (Join-Path $Root "infra\chaos\chaos.py") @Rest
    }
    "edge-check" {
        # Tests the edge itself (microcache, limiter, public surface) and the drain through it. Uses nginx.
        & $Py $Run down
        & $Py $Run up --replicas 3 --mail file --edge nginx --sim
        & $Py $Run exec -- python scripts/edge_test.py
        & $Py $Run exec -- python scripts/drain_test.py
        & $Py $Run down
        & $Py $Run up --replicas 3
    }
    "get-redis" { & $Py (Join-Path $Root "infra\local\get_redis.py") }
    "check"  {
        # Restarts the stack with the FILE mail outbox first: these scripts register made-up addresses,
        # which must never be emailed for real. Afterwards run `fd.ps1 up` again for real email.
        & $Py $Run down
        & $Py $Run up --replicas 3 --mail file
        $tsx = (Resolve-Path (Join-Path $Root "..\frontend\node_modules\.bin\tsx.cmd")).Path
        $fe  = (Resolve-Path (Join-Path $Root "..\frontend\tsconfig.json")).Path
        & $Py $Run exec -- python scripts/local_e2e.py
        & $Py $Run exec -- $tsx --tsconfig $fe scripts/frontend_contract_check.ts
        & $Py $Run exec -- node scripts/browser_e2e.mjs
        & $Py $Run exec -- python scripts/smoke_attacks.py
    }
}
