from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from fairdrop_sim.models import CHART_IDS, ChartDataset, Experiment, Results, Scenario
from fairdrop_sim.samples import sample_charts, sample_results, write_samples

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT.parent / "docs" / "sample_results"


def test_example_scenario_validates():
    sc = Scenario.from_yaml(ROOT / "experiments" / "example_mock_smoke.yaml")
    assert sc.target == "mock" and sc.bot_identities == 220


def test_scenario_rejects_unknown_keys_layers_and_profiles():
    with pytest.raises(ValidationError):
        Scenario.model_validate({"name": "x", "typo_field": 1})
    with pytest.raises(ValidationError):
        Scenario.model_validate({"name": "x", "event": {"defences": {"preset": "custom", "layers": {"magic": {}}}}})
    with pytest.raises(ValidationError):
        Scenario.model_validate({"name": "x", "attackers": [{"profile": "teleporter"}]})


def test_results_roundtrip_and_marking():
    for r in sample_results():
        assert Results.model_validate_json(r.model_dump_json()) == r
        assert r.synthetic and r.target == "mock" and r.run_id.startswith("synthetic-")


def test_samples_are_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write_samples(a)
    write_samples(b)
    files = list(a.rglob("*.json"))
    assert len(files) >= 15
    for f in files:
        assert f.read_bytes() == (b / f.relative_to(a)).read_bytes(), f


def test_all_chart_ids_present_marked_and_consistent():
    charts = sample_charts()
    assert {c.chart_id for c in charts} == set(CHART_IDS)
    for c in charts:
        assert c.synthetic and "SYNTHETIC" in (c.notes or "")
        for s in c.series:
            for p in s.points:
                if p.ci_low is not None:
                    assert p.ci_low <= p.y <= p.ci_high


@pytest.mark.skipif(not SAMPLES.exists(), reason="run `fdsim samples` first")
def test_published_samples_validate():
    for f in (SAMPLES / "results").glob("*.json"):
        assert Results.model_validate_json(f.read_text(encoding="utf-8")).synthetic is True
    for f in (SAMPLES / "charts").glob("*.json"):
        assert ChartDataset.model_validate_json(f.read_text(encoding="utf-8")).synthetic is True
    for e in json.loads((SAMPLES / "experiments.json").read_text(encoding="utf-8")):
        assert Experiment.model_validate(e).synthetic is True
