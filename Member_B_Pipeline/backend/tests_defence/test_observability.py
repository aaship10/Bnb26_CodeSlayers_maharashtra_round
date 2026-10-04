"""Metrics: correctness of the numbers, bounded labels, and the committed dashboards staying in step with the code."""
import json
import re
import uuid
from pathlib import Path

import pytest
import yaml
from app.defence import metrics
from app.defence.metrics import ALL_METRIC_NAMES, REGISTRY
from app.defence.pow import protocol
from app.defence.settings import get_settings

ROOT = Path(__file__).resolve().parents[2]
EV = "11111111-1111-1111-1111-111111111111"


def val(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


# --------------------------------------------------------------- the registry and the dashboards
def _family_names():
    return {m.name for m in REGISTRY.collect()}


def test_the_documented_metric_list_is_exactly_what_is_registered():
    # prometheus_client names a Counter family without its `_total` suffix, so normalise the documented names
    documented = {re.sub(r"_total$", "", n) for n in ALL_METRIC_NAMES}
    assert _family_names() == documented


def test_dashboards_only_query_metrics_that_exist():
    dash = json.loads((ROOT / "infra" / "grafana" / "dashboards" / "fairdrop.json").read_text(encoding="utf-8"))
    ids = [p["id"] for p in dash["panels"]]
    assert len(ids) == len(set(ids)) and dash["uid"] == "fairdrop-defence"
    used = set()
    for p in dash["panels"]:
        assert p["targets"], p["title"]
        for t in p["targets"]:
            used |= set(re.findall(r"fd_[a-z_]+", t["expr"]))
    assert used, "no fd_ metrics referenced at all"
    unknown = {re.sub(r"_(bucket|sum|count)$", "", n) for n in used} - ALL_METRIC_NAMES
    assert not unknown, f"dashboard queries metrics that do not exist: {unknown}"


def test_the_ops_page_reads_only_metrics_that_exist():
    html = (ROOT / "infra" / "local" / "ops" / "index.html").read_text(encoding="utf-8")
    used = {re.sub(r"_(bucket|sum|count)$", "", n) for n in re.findall(r"fd_[a-z_]+", html)}
    assert used and used <= ALL_METRIC_NAMES, used - ALL_METRIC_NAMES


def test_prometheus_config_targets_the_replicas_not_the_gateway():
    cfg = yaml.safe_load((ROOT / "infra" / "prometheus" / "prometheus.yml").read_text(encoding="utf-8"))
    job = cfg["scrape_configs"][0]
    assert job["metrics_path"] == "/metrics" and all(not t.endswith(":8080") for t in job["static_configs"][0]["targets"])


def test_no_metric_name_or_label_needs_ground_truth():
    names = " ".join(_family_names())
    labels = {l for m in REGISTRY.collect() for s in m.samples for l in s.labels}
    assert "sim_label" not in names and not any("label" in l.lower() and "sim" in l.lower() for l in labels)


# --------------------------------------------------------------------------- the endpoint
async def test_metrics_endpoint_and_route_templates(stub, client):
    r = await client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain") and "fd_http_requests_total" in r.text
    _, h = await stub.user()
    before = val("fd_http_requests_total", method="GET", route="/events/{event_id}/status", status="200")
    await client.get(f"/events/{EV}/status", headers=h)
    await client.get(f"/events/{uuid.uuid4()}/status", headers=h)  # a different id: must land in the SAME series
    assert val("fd_http_requests_total", method="GET", route="/events/{event_id}/status", status="200") == before + 1
    assert EV not in (await client.get("/metrics")).text  # ids never become label values


async def test_label_cardinality_is_bounded_under_junk_traffic(client):
    for i in range(60):
        await client.get(f"/scan/{uuid.uuid4()}/{i}")
    text = (await client.get("/metrics")).text
    assert len(re.findall(r'route="unmatched"', text)) <= 2  # one counter series (per status), however many paths
    assert "/scan/" not in text
    assert 'fd_http_request_duration_seconds_count{route="unmatched"}' not in text  # no latency series for junk


async def test_metrics_endpoint_is_not_in_the_public_api_docs(client):
    assert "/metrics" not in (await client.get("/openapi.json")).json()["paths"]


async def test_labels_never_contain_personal_data(stub, client, rt):
    _, h = await stub.user()
    await client.post("/auth/register", json={"email": "someone.private@example-college.edu", "display_name": "P", "hp": ""})
    await client.get(f"/events/{EV}/status", headers=h)
    text = (await client.get("/metrics")).text
    assert "someone.private" not in text and "@" not in text
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", text)  # no uuid anywhere


# ------------------------------------------------------------------ the defence counters move
async def test_gate_and_challenge_counters(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit+pow"})
    uid, h = await stub.user()
    issued0 = val("fd_challenges_issued_total", type="pow")
    solved0 = val("fd_challenges_solved_total", type="pow")
    chal0 = val("fd_gate_decisions_total", action="CHALLENGE", layer="pow", weight="none")
    allow0 = val("fd_gate_decisions_total", action="ALLOW", layer="none", weight="1.0")
    bad0 = val("fd_challenges_failed_total", type="pow", reason="bad_solution")

    r = await client.post(f"/events/{EV}/enter", headers=h)
    ch = r.json()["details"]["challenge"]
    wrong = next(str(n) for n in range(10_000) if not protocol.solution_ok(ch["id"], str(n), ch["pow"]["difficulty_bits"]))
    r2 = await client.post(f"/events/{EV}/enter", headers={**h, "X-Challenge-Id": ch["id"], "X-Challenge-Solution": wrong})
    ch2 = r2.json()["details"]["challenge"]
    sol = protocol.solve(ch2["pow"]["prefix"], ch2["pow"]["difficulty_bits"])
    ok = await client.post(f"/events/{EV}/enter", headers={**h, "X-Challenge-Id": ch2["id"], "X-Challenge-Solution": sol})
    assert ok.status_code == 201

    assert val("fd_challenges_issued_total", type="pow") == issued0 + 2
    assert val("fd_challenges_solved_total", type="pow") == solved0 + 1
    assert val("fd_challenges_failed_total", type="pow", reason="bad_solution") == bad0 + 1
    assert val("fd_gate_decisions_total", action="CHALLENGE", layer="pow", weight="none") == chal0 + 2
    assert val("fd_gate_decisions_total", action="ALLOW", layer="none", weight="1.0") == allow0 + 1  # ALLOW carries no layer


async def test_forgery_and_rate_limit_counters(stub, client, rt):
    await stub.set_defences({"preset": "rate_limit+pow"})
    uid, h = await stub.user()
    forged0 = val("fd_challenges_failed_total", type="any", reason="forged")
    ch = (await client.post(f"/events/{EV}/enter", headers=h)).json()["details"]["challenge"]
    r = await client.post(f"/events/{EV}/enter", headers={**h, "X-Challenge-Id": ch["id"][:-1] + ("0" if ch["id"][-1] != "0" else "1"), "X-Challenge-Solution": "1"})
    assert r.json()["code"] == "REJECTED"
    assert val("fd_challenges_failed_total", type="any", reason="forged") == forged0 + 1
    assert val("fd_gate_decisions_total", action="REJECT", layer="pow", weight="none") >= 1

    rl0 = val("fd_rate_limited_total", endpoint="status", scope="identity")
    for _ in range(6):
        await client.get(f"/events/{EV}/status", headers=h)
    assert val("fd_rate_limited_total", endpoint="status", scope="identity") > rl0


async def test_risk_band_counter(stub, client, rt):
    await stub.set_defences({"preset": "custom", "layers": {"signals": {"enabled": True}, "risk": {"enabled": True}}})
    _, h = await stub.user()
    before = val("fd_risk_band_total", band="low")
    await client.post(f"/events/{EV}/enter", headers={**h, "User-Agent": "Mozilla/5.0 Chrome/126", "Accept-Language": "en"})
    assert val("fd_risk_band_total", band="low") == before + 1


async def test_decision_log_gauges_read_the_writers_counters(rt):
    rt.decisions.dropped = 7
    rt.decisions.written = 11
    metrics.bind_decision_log(rt.decisions)
    assert val("fd_decision_log_dropped") == 7 and val("fd_decision_log_written") == 11
    rt.decisions.dropped = 9
    assert val("fd_decision_log_dropped") == 9  # read at scrape time, not copied
    assert get_settings() is not None
