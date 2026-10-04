"""Single-run summary (Stage 2). Stage 4's metrics engine turns many of these into a
Results file with CIs; this is the per-run raw view, printed after `fdsim load`."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from fairdrop_sim.engine.recorder import Recorder
from fairdrop_sim.models.scenario import Scenario

LAG_WARN_MS = 50.0


def _pcts(h, scale: float = 1 / 1000) -> dict[str, float | int | None]:
    n = h.get_total_count()
    if n == 0:
        return {"p50": None, "p95": None, "p99": None, "max": None, "n": 0}
    return {
        "p50": round(h.get_value_at_percentile(50) * scale, 2),
        "p95": round(h.get_value_at_percentile(95) * scale, 2),
        "p99": round(h.get_value_at_percentile(99) * scale, 2),
        "max": round(h.get_max_value() * scale, 2),
        "n": n,
    }


def summarize(*, sc: Scenario, run_id: str, run_index: int, run_seed: int, seed_hex: str, target: str,
              event_id: str, rec: Recorder, humans, outcomes: list[dict], server: dict[str, Any],
              invariants: dict[str, Any], stats: dict[str, Any], draw_info: dict[str, Any], crashed: int,
              crash_samples: list[str], attackers: list[dict] | None = None,
              bots_by_attacker: dict | None = None) -> dict[str, Any]:
    attackers = attackers or []
    bots_by_attacker = bots_by_attacker or {}
    n = len(outcomes)
    entered = sum(o["entered"] for o in outcomes)
    claimed = sum(o["claimed"] for o in outcomes)
    entries = server["entries"]
    srv_states = Counter(entries["state"]) if len(entries) else Counter()
    draw_states = Counter(entries["draw_state"].dropna()) if len(entries) else Counter()
    nat = [o for o in outcomes if humans.nat_group[o["idx"]] >= 0]

    # Fairness view: split entries into bot vs human by ground-truth ids (simulator-side).
    bot_ids: set[str] = set()
    for a in attackers:
        bot_ids.update(bots_by_attacker[a["attacker_index"]].user_ids)
    ent_ids = set(entries["user_id"]) if len(entries) else set()
    bot_entry_ids = ent_ids & bot_ids
    final = entries[entries["state"] == "CLAIMED"] if len(entries) else entries
    final_ids = set(final["user_id"]) if len(final) else set()
    bot_seats = len(final_ids & bot_ids)
    all_seats = len(final_ids)
    draw_win_ids = set(entries[entries["draw_state"].isin(["WON", "CLAIMED"])]["user_id"]) if len(entries) else set()
    fairness = {
        "all_entries": len(ent_ids),
        "bot_entries": len(bot_entry_ids),
        "human_entries": len(ent_ids) - len(bot_entry_ids),
        "bot_entrant_share": round(len(bot_entry_ids) / len(ent_ids), 6) if ent_ids else None,
        "all_final_seats": all_seats,
        "bot_final_seats": bot_seats,
        "human_final_seats": all_seats - bot_seats,
        "bot_seat_share": round(bot_seats / all_seats, 6) if all_seats else None,
        "bot_draw_winners": len(draw_win_ids & bot_ids),
    }
    attacker_summaries = []
    for a in attackers:
        ids = set(bots_by_attacker[a["attacker_index"]].user_ids)
        seats = len(final_ids & ids)
        atk_entries = len(ent_ids & ids)
        outs = a["outcomes"]
        attacker_summaries.append({
            "attacker_index": a["attacker_index"],
            "profile": a["profile"],
            "identities": len(ids),
            "entered": sum(o["entered"] for o in outs),
            "server_entries": atk_entries,
            "draw_winners": len(draw_win_ids & ids),
            "seats_won": seats,
            "claimed_client": sum(o["claimed"] for o in outs),
            "challenges_seen": sum(o.get("challenge_seen", 0) for o in outs),
            "cost": a["cost"].summary(a["config"].cost, seats),
        })

    total = rec.count()
    duration = (rec.last_recv - rec.first_intended) if rec.first_intended is not None and rec.last_recv is not None else 0.0
    per_sec = Counter()
    for k, c in rec.timeline.items():
        per_sec[int(k.split("|", 1)[0])] += c
    spike_s = max(1, int(round(sc.event.window_seconds * sc.legit.arrival.spike_window_fraction)))
    spike_total = sum(c for s, c in per_sec.items() if 0 <= s < spike_s)
    spike_bad = 0
    for k, c in rec.timeline.items():
        s, _key, out = k.split("|", 2)
        if 0 <= int(s) < spike_s and out in ("5xx", "TIMEOUT", "CONN_ERROR"):
            spike_bad += c

    lag = _pcts(rec.sched_lag)
    warnings: list[str] = []
    if target == "mock":
        warnings.append("MOCK target: development double, not evidence about the real system")
    if lag["p99"] is not None and lag["p99"] > LAG_WARN_MS:
        warnings.append(f"load generator lagged (scheduler lag p99 {lag['p99']} ms > {LAG_WARN_MS} ms): "
                        "open-loop latencies include client-side delay; add procs or lower max load")
    n_to, n_conn = rec.count(outcome="TIMEOUT"), rec.count(outcome="CONN_ERROR")
    if n_to or n_conn:
        warnings.append(f"{n_to:,} requests timed out, {n_conn:,} connection errors")
    if crashed:
        warnings.append(f"{crashed} simulated users crashed (bug in the simulator): {crash_samples[:3]}")
    if not invariants.get("passed"):
        warnings.append(f"INVARIANTS FAILED: {invariants}")

    by_ep = {}
    for ep in ("enter", "status", "claim"):
        by_ep[ep] = {
            "requests": rec.count(ep),
            "ok": rec.count(ep, outcome="ok"),
            "429": rec.count(ep, outcome="429"),
            "4xx": rec.count(ep, outcome="4xx"),
            "5xx": rec.count(ep, outcome="5xx"),
            "timeout": rec.count(ep, outcome="TIMEOUT"),
            "conn_error": rec.count(ep, outcome="CONN_ERROR"),
            "open_loop_ms": _pcts(rec.hist(ep)),
            "service_ms": _pcts(rec.hist(ep, kind="service")),
        }
    codes = Counter()
    for k, c in rec.counts.items():
        out = k.split("|", 1)[1]
        if out.startswith("4xx:"):
            codes[out[4:]] += c

    return {
        "run_id": run_id,
        "scenario": sc.name,
        "run_index": run_index,
        "run_seed": run_seed,
        "server_seed_hex": seed_hex,
        "target": target,
        "synthetic": False,
        "event_id": event_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "population": {"legit": n, "bots": len(attackers), "bot_identities": len(bot_ids), "nat_users": len(nat)},
        "load": {
            "requests": total,
            "duration_s": round(duration, 2),
            "throughput_rps": round(total / duration, 1) if duration else None,
            "peak_offered_rps": max(per_sec.values()) if per_sec else 0,
            "spike_seconds": spike_s,
            "spike_requests": spike_total,
            "availability_spike": round(1 - spike_bad / spike_total, 5) if spike_total else None,
            "scheduler_lag_ms": lag,
            "error_codes_4xx": dict(codes),
        },
        "endpoints": by_ep,
        "outcomes": {
            "humans": n,
            "entered_client": entered,
            "entered_server": fairness["human_entries"],
            "human_entry_success_rate": round(entered / n, 5) if n else None,
            "nat_entry_success_rate": round(sum(o["entered"] for o in nat) / len(nat), 5) if nat else None,
            "draw_states": dict(draw_states),
            "final_states": dict(srv_states),
            "claimed_client": claimed,
            "human_win_prob": round(fairness["human_final_seats"] / n, 6) if n else None,
            "gave_up": dict(Counter(o["gave_up"] for o in outcomes if o["gave_up"])),
            "requests_per_human": round(total / n, 2) if n else None,
        },
        "fairness": fairness,
        "attackers": attacker_summaries,
        "integrity": {
            **invariants.get("checks", {}),
            "passed": bool(invariants.get("passed")),
            "draw_verified": (server.get("verify") or {}).get("verified"),
        },
        "draw": draw_info,
        "server_stats": stats,
        "warnings": warnings,
        "scenario_config": sc.model_dump(mode="json"),
    }


def format_summary(s: dict[str, Any]) -> str:
    L = s["load"]
    o = s["outcomes"]
    lines = [
        f"run {s['run_id']}  target={s['target'].upper()}  seed={s['run_seed']}",
        f"  users: {o['humans']:,} legit ({s['population']['nat_users']:,} behind NAT)   "
        f"requests: {L['requests']:,} in {L['duration_s']}s = {L['throughput_rps']} rps "
        f"(peak offered {L['peak_offered_rps']:,}/s)",
        f"  scheduler lag ms: p50 {L['scheduler_lag_ms']['p50']}  p99 {L['scheduler_lag_ms']['p99']}  "
        f"max {L['scheduler_lag_ms']['max']}",
        "  endpoint   requests      ok    429    4xx    5xx  t/o conn | open-loop ms p50/p95/p99  | service ms p50/p95/p99",
    ]
    for ep, e in s["endpoints"].items():
        ol, sv = e["open_loop_ms"], e["service_ms"]
        lines.append(
            f"  {ep:<8} {e['requests']:>9,} {e['ok']:>8,} {e['429']:>6,} {e['4xx']:>6,} {e['5xx']:>6,} "
            f"{e['timeout']:>4,} {e['conn_error']:>4,} | {ol['p50']!s:>7} {ol['p95']!s:>8} {ol['p99']!s:>8} | "
            f"{sv['p50']!s:>6} {sv['p95']!s:>7} {sv['p99']!s:>7}")
    lines += [
        f"  entry success (humans): {o['human_entry_success_rate']}  (NAT users: {o['nat_entry_success_rate']})   "
        f"server entries: {o['entered_server']:,}",
        f"  after draw: {o['draw_states']}   final: {o['final_states']}",
        f"  human win prob: {o['human_win_prob']}   availability during spike: {L['availability_spike']}",
    ]
    f = s.get("fairness", {})
    if s["population"]["bots"]:
        lines.append(
            f"  FAIRNESS: bot seat share {f.get('bot_seat_share')} ({f.get('bot_final_seats')}/{f.get('all_final_seats')} seats)"
            f"  |  bot entrant share {f.get('bot_entrant_share')} ({f.get('bot_entries')}/{f.get('all_entries')})"
            f"  |  bot draw winners {f.get('bot_draw_winners')}")
        for a in s.get("attackers", []):
            ps = a["cost"]["per_seat"]
            lines.append(
                f"    {a['profile']:<18} ids={a['identities']:>6,} entered={a['entered']:>6,} "
                f"seats={a['seats_won']:>4}  challenges={a['challenges_seen']:>5}  "
                f"cost/seat: req={ps['requests']} acct={ps['accounts']} pow={ps['pow_hashes']} $={ps['usd_modelled']}")
    lines.append(f"  integrity: {'PASSED' if s['integrity']['passed'] else 'FAILED'} {s['integrity']}")
    lines += [f"  ! {w}" for w in s["warnings"]]
    lines.append(f"  artifacts: {s.get('artifacts')}")
    return "\n".join(lines)
