"""`fdsim` command line: mock, smoke, load, samples, schemas, validate."""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"


def _cmd_mock(a: argparse.Namespace) -> int:
    import uvicorn

    from mock_server import create_app

    print(f"Fair Drop MOCK target on http://{a.host}:{a.port}  (dev double; results against it are target=mock)")
    uvicorn.run(create_app(), host=a.host, port=a.port, log_level="warning", access_log=False,
                timeout_keep_alive=75)
    return 0


def _cmd_samples(a: argparse.Namespace) -> int:
    from fairdrop_sim.samples import write_samples

    for p in write_samples(Path(a.out)):
        print(p.relative_to(REPO_ROOT) if p.is_relative_to(REPO_ROOT) else p)
    return 0


def _cmd_schemas(a: argparse.Namespace) -> int:
    from fairdrop_sim.samples import write_schemas

    for p in write_schemas(Path(a.out)):
        print(p)
    return 0


def _cmd_validate(a: argparse.Namespace) -> int:
    from pydantic import ValidationError

    from fairdrop_sim.models import ChartDataset, Experiment, Results, Scenario

    bad = 0
    for f in a.files:
        path = Path(f)
        try:
            if path.suffix in (".yaml", ".yml"):
                Scenario.from_yaml(path)
                kind = "scenario"
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    [Experiment.model_validate(x) for x in data]
                    kind = "experiments"
                elif "chart_id" in data:
                    ChartDataset.model_validate(data)
                    kind = "chart"
                else:
                    Results.model_validate(data)
                    kind = "results"
            print(f"OK    {kind:<11} {f}")
        except (ValidationError, ValueError, OSError) as e:
            bad += 1
            print(f"FAIL  {f}\n{e}")
    return 1 if bad else 0


def _cmd_smoke(a: argparse.Namespace) -> int:
    """Walk one event through its whole lifecycle against a running target."""
    import httpx

    admin = {"X-Admin-Token": a.admin_token}
    c = httpx.Client(base_url=a.base_url, timeout=10)

    def show(label: str, r: httpx.Response) -> dict:
        body = r.json()
        print(f"{r.status_code}  {label:<28} {json.dumps(body)[:150]}")
        return body

    ev_id = f"evt_smoke_{uuid.uuid4().hex[:6]}"
    show("create event", c.post("/admin/events", headers=admin, json={
        "id": ev_id, "inventory": 2, "window_seconds": 30, "claim_ttl_seconds": 30, "server_seed_hex": "ab" * 32}))
    users = [f"user-{i}" for i in range(5)]
    show("enter before open", c.post(f"/events/{ev_id}/enter", headers={"X-User-Id": users[0]}))
    show("open", c.post(f"/admin/events/{ev_id}/open", headers=admin))
    for u in users:
        show(f"enter {u}", c.post(f"/events/{ev_id}/enter", headers={"X-User-Id": u}))
    show("enter again (idempotent)", c.post(f"/events/{ev_id}/enter", headers={"X-User-Id": users[0]}))
    show("close", c.post(f"/admin/events/{ev_id}/close", headers=admin))
    show("draw", c.post(f"/admin/events/{ev_id}/draw", headers=admin))
    winner = None
    for u in users:
        st = show(f"status {u}", c.get(f"/events/{ev_id}/status", headers={"X-User-Id": u}))
        if st.get("state") == "WON" and winner is None:
            winner = u
    key = {"Idempotency-Key": "smoke-key-0001"}
    show(f"claim {winner}", c.post(f"/events/{ev_id}/claim", headers={"X-User-Id": winner, **key}))
    show("claim replay (same key)", c.post(f"/events/{ev_id}/claim", headers={"X-User-Id": winner, **key}))
    loser = next(u for u in users if u != winner)
    show(f"claim {loser}", c.post(f"/events/{ev_id}/claim", headers={"X-User-Id": loser,
                                                                    "Idempotency-Key": "smoke-key-0002"}))
    inv = show("invariants", c.get(f"/admin/events/{ev_id}/invariants", headers=admin))
    ver = show("verify draw", c.get(f"/__mock/events/{ev_id}/verify", headers=admin))
    ok = inv.get("passed") is True and ver.get("verified") is True
    print("SMOKE", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def _cmd_load(a: argparse.Namespace) -> int:
    """Run ONE run of a scenario and print the summary (Stage 2). Stage 5 adds repeats."""
    import asyncio
    import os

    from fairdrop_sim.engine.run import DEFAULT_OUT, TargetConfig, execute_run
    from fairdrop_sim.engine.summary import format_summary
    from fairdrop_sim.models import Scenario

    sc = Scenario.from_yaml(a.scenario)
    patch: dict = {}
    if a.users is not None:
        patch.setdefault("legit", sc.legit.model_dump())["count"] = a.users
    if a.procs is not None:
        patch.setdefault("load", sc.load.model_dump())["procs"] = a.procs
    if patch:
        sc = Scenario.model_validate({**sc.model_dump(), **patch})
    tgt = TargetConfig(
        base_url=a.base_url,
        admin_token=a.admin_token or os.environ.get("ADMIN_TOKEN", "dev-admin-token"),
        sim_key=a.sim_key or os.environ.get("SIM_KEY", "dev-sim-key"),
    )
    summary = asyncio.run(execute_run(sc, a.run_index, tgt, Path(a.out) if a.out else DEFAULT_OUT))
    print()
    print(format_summary(summary))
    return 0 if summary["integrity"]["passed"] else 2


def _cmd_metrics(a: argparse.Namespace) -> int:
    """Aggregate run directories (repeats of one scenario) into a Results JSON."""
    import json

    from fairdrop_sim.metrics.aggregate import build_results

    results = build_results(a.run_dirs, run_id=a.run_id, scenario_id=a.scenario_id,
                            boot_resamples=a.resamples, perm_resamples=a.resamples)
    data = results.model_dump(mode="json")
    out = json.dumps(data, indent=2, ensure_ascii=False)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(out + "\n", encoding="utf-8")
        print(f"wrote {a.out}")
    else:
        print(out)
    m = data["metrics"]
    f = m["fairness"]
    st = lambda x: (f"{x['mean']:.4f} [{x['ci_low']:.4f},{x['ci_high']:.4f}] n={x['n']}"  # noqa: E731
                    if isinstance(x, dict) else str(x))
    print("\n--- summary ---", file=__import__("sys").stderr)
    for key in ("bot_seat_share", "bot_entrant_share", "human_win_prob", "human_entry_success_rate",
                "arrival_time_correlation", "jain_index", "gini"):
        if f.get(key) is not None:
            print(f"  {key:<26} {st(f[key])}", file=__import__("sys").stderr)
    print(f"  arrival_perm_p             {f.get('arrival_time_perm_p')}", file=__import__("sys").stderr)
    print(f"  integrity passed           {m['integrity']['passed']}", file=__import__("sys").stderr)
    return 0


def _cmd_experiment(a: argparse.Namespace) -> int:
    """Run an experiment spec (E1..E8): execute every cell's repeats, aggregate, build the chart."""
    import asyncio
    import os

    from fairdrop_sim.engine.run import TargetConfig
    from fairdrop_sim.runner.experiment import ExperimentSpec, MockNotAllowed, run_experiment

    spec_path = Path(a.spec)
    spec = ExperimentSpec.from_yaml(spec_path)
    if a.repeats:
        spec = spec.model_copy(update={"repeats": a.repeats})
    tgt = TargetConfig(
        base_url=a.base_url,
        admin_token=a.admin_token or os.environ.get("ADMIN_TOKEN", "dev-admin-token"),
        sim_key=a.sim_key or os.environ.get("SIM_KEY", "dev-sim-key"),
    )
    out_root = Path(a.out) if a.out else (REPO_ROOT / "simulator" / "results" / "experiments")
    try:
        out = asyncio.run(run_experiment(spec, tgt, out_root, spec_path=spec_path, allow_mock=a.allow_mock))
    except MockNotAllowed as e:
        print(f"REFUSED: {e}\n(pass --allow-mock to run a final experiment against the mock)")
        return 2
    print(f"\n{spec.id} complete: {len(out.cells)} cells, chart {spec.chart_id}.json, "
          f"integrity {'PASSED' if out.integrity_passed else 'FAILED'}")
    print(f"artifacts: {out.out_dir}")
    return 0 if out.integrity_passed else 2


def _cmd_serve(a: argparse.Namespace) -> int:
    """The /sim HTTP service D's panel drives (port 8100). Spawns its own mock for target=mock."""
    import uvicorn

    from fairdrop_sim.service.app import create_app

    print(f"Fair Drop simulator service on http://{a.host}:{a.port}  (routes at / and /sim/)")
    print("  mock runs: uses FD_MOCK_URL if set, else starts its own dev mock.  real runs: FD_REAL_URL "
          "(default http://127.0.0.1:8000)")
    uvicorn.run(create_app(), host=a.host, port=a.port, log_level="warning", access_log=False, timeout_keep_alive=75)
    return 0


def _cmd_suite(a: argparse.Namespace) -> int:
    """Run experiment specs E1..E8 from scratch with their fixed seeds (regenerates every chart)."""
    import asyncio
    import os

    from fairdrop_sim.engine.run import TargetConfig
    from fairdrop_sim.runner.experiment import ExperimentSpec, MockNotAllowed, run_experiment

    exp_dir = REPO_ROOT / "simulator" / "experiments"
    wanted = [x.strip().upper() for x in a.only.split(",")] if a.only else None
    specs = [p for p in sorted(exp_dir.glob("E[0-9]*.yaml")) if wanted is None or p.stem.upper() in wanted]
    if not specs:
        print("no matching experiment specs")
        return 2
    tgt = TargetConfig(base_url=a.base_url,
                       admin_token=a.admin_token or os.environ.get("ADMIN_TOKEN", "dev-admin-token"),
                       sim_key=a.sim_key or os.environ.get("SIM_KEY", "dev-sim-key"))
    out_root = Path(a.out) if a.out else REPO_ROOT / "simulator" / "results" / "experiments"
    worst = 0
    for path in specs:
        spec = ExperimentSpec.from_yaml(path)
        if a.repeats:
            spec = spec.model_copy(update={"repeats": a.repeats})
        try:
            out = asyncio.run(run_experiment(spec, tgt, out_root, spec_path=path, allow_mock=a.allow_mock))
        except MockNotAllowed as e:
            print(f"REFUSED {spec.id}: {e}")
            worst = max(worst, 2)
            continue
        if not out.integrity_passed:
            worst = max(worst, 2)
    print(f"\nsuite finished ({len(specs)} experiments); " + ("integrity PASSED" if worst == 0 else "SEE ABOVE"))
    return worst


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fdsim", description="Fair Drop simulator (Member C)")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mock", help="run the in-memory MOCK target (dev double)")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8200)
    m.set_defaults(fn=_cmd_mock)

    s = sub.add_parser("samples", help="regenerate SYNTHETIC sample results + chart datasets + schemas")
    s.add_argument("--out", default=str(DOCS / "sample_results"))
    s.set_defaults(fn=_cmd_samples)

    sc = sub.add_parser("schemas", help="export JSON Schemas for results/chart/experiment/scenario")
    sc.add_argument("--out", default=str(DOCS / "sample_results" / "schema"))
    sc.set_defaults(fn=_cmd_schemas)

    v = sub.add_parser("validate", help="validate results/chart/experiments JSON or scenario YAML files")
    v.add_argument("files", nargs="+")
    v.set_defaults(fn=_cmd_validate)

    sm = sub.add_parser("smoke", help="lifecycle smoke test against a running target")
    sm.add_argument("--base-url", default="http://127.0.0.1:8200")
    sm.add_argument("--admin-token", default="dev-admin-token")
    sm.set_defaults(fn=_cmd_smoke)

    sv = sub.add_parser("serve", help="run the /sim HTTP service on :8100 for the dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8100)
    sv.set_defaults(fn=_cmd_serve)

    su = sub.add_parser("suite", help="run experiments E1..E8 from scratch (fixed seeds), regenerating every chart")
    su.add_argument("--only", help="comma list, e.g. E1,E2")
    su.add_argument("--base-url", default="http://127.0.0.1:8200")
    su.add_argument("--allow-mock", action="store_true")
    su.add_argument("--repeats", type=int)
    su.add_argument("--admin-token")
    su.add_argument("--sim-key")
    su.add_argument("--out")
    su.set_defaults(fn=_cmd_suite)

    ex = sub.add_parser("experiment", help="run an experiment spec (E1..E8): cells x repeats -> Results + chart")
    ex.add_argument("spec")
    ex.add_argument("--base-url", default="http://127.0.0.1:8200")
    ex.add_argument("--allow-mock", action="store_true", help="allow a final experiment to run against the mock")
    ex.add_argument("--repeats", type=int, help="override the spec's repeats")
    ex.add_argument("--admin-token")
    ex.add_argument("--sim-key")
    ex.add_argument("--out", help="experiment output root (default simulator/results/experiments)")
    ex.set_defaults(fn=_cmd_experiment)

    me = sub.add_parser("metrics", help="aggregate run directories into a Results JSON (CIs, correlation, Jain/Gini)")
    me.add_argument("run_dirs", nargs="+")
    me.add_argument("--out", help="write Results JSON here (else stdout)")
    me.add_argument("--run-id")
    me.add_argument("--scenario-id")
    me.add_argument("--resamples", type=int, default=10000)
    me.set_defaults(fn=_cmd_metrics)

    ld = sub.add_parser("load", help="run one run of a scenario (open-loop) and print the summary")
    ld.add_argument("scenario")
    ld.add_argument("--base-url", default="http://127.0.0.1:8200")
    ld.add_argument("--run-index", type=int, default=0)
    ld.add_argument("--users", type=int, help="override legit.count")
    ld.add_argument("--procs", type=int, help="override load.procs")
    ld.add_argument("--admin-token")
    ld.add_argument("--sim-key")
    ld.add_argument("--out", help="artifact directory (default simulator/results/runs)")
    ld.set_defaults(fn=_cmd_load)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
