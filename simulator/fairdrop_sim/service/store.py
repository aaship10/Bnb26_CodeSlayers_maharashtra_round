"""Experiments and charts the service exposes.

Real experiments: simulator/results/experiments/<id>/ (written by `fdsim experiment`), with an
experiment.json index and one <chart_id>.json dataset. Synthetic samples (docs/sample_results,
ids prefixed "synthetic-") are listed too, unless disabled, so the dashboard has something to
draw before any real run. They are flagged synthetic everywhere and watermarked in PNGs.

A chart is addressed by (chart_id, experiment). Two experiments may share a chart_id (E5/E7,
E6/E8), which is why the experiment is part of the lookup rather than the id alone.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fairdrop_sim.charts.render import render_png
from fairdrop_sim.models import ChartDataset


class ChartStore:
    def __init__(self, experiments_dir: Path, samples_dir: Path | None, include_samples: bool = True):
        self.exp_dir = experiments_dir
        self.samples = samples_dir if include_samples and samples_dir and samples_dir.exists() else None
        self._png_cache: dict[tuple[str, float], bytes] = {}

    # ------------------------------------------------------------------ experiments

    def experiments(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.exp_dir.exists():
            for d in sorted(self.exp_dir.iterdir()):
                meta = d / "experiment.json"
                if meta.exists():
                    try:
                        out.append(json.loads(meta.read_text(encoding="utf-8")))
                    except (OSError, ValueError):
                        continue
        if self.samples and (self.samples / "experiments.json").exists():
            out += json.loads((self.samples / "experiments.json").read_text(encoding="utf-8"))
        return sorted(out, key=lambda e: e.get("created_at", ""), reverse=True)

    def experiment(self, exp_id: str) -> dict[str, Any] | None:
        return next((e for e in self.experiments() if e["id"] == exp_id), None)

    # ------------------------------------------------------------------ charts

    def _chart_path(self, chart_id: str, experiment: str | None) -> Path | None:
        if experiment and "/" not in experiment and "\\" not in experiment and ".." not in experiment:
            p = self.exp_dir / experiment / f"{chart_id}.json"
            if p.exists():
                return p
        if self.samples:  # sample experiments (synthetic-*) or no experiment given
            if experiment is None or experiment.startswith("synthetic-"):
                p = self.samples / "charts" / f"{chart_id}.json"
                if p.exists():
                    return p
        return None

    def chart(self, chart_id: str, experiment: str | None) -> tuple[dict[str, Any], Path] | None:
        if "/" in chart_id or "\\" in chart_id or ".." in chart_id:
            return None
        p = self._chart_path(chart_id, experiment)
        if p is None:
            return None
        return json.loads(p.read_text(encoding="utf-8")), p

    def png(self, chart_id: str, experiment: str | None) -> bytes | None:
        found = self.chart(chart_id, experiment)
        if found is None:
            return None
        data, path = found
        key = (str(path), path.stat().st_mtime)
        if key not in self._png_cache:
            self._png_cache[key] = render_png(ChartDataset.model_validate(data))
        return self._png_cache[key]
