"""Chart dataset JSON (section 10). One file per chart, next to its PNG."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CHART_IDS = (
    "bot_share_vs_request_multiplier",
    "bot_share_vs_identities",
    "latency_percentiles_normal_vs_attack",
    "detection_precision_recall_by_layer",
    "ablation_bot_share",
    "human_win_prob_under_attack",
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Point(_Model):
    x: float | str
    y: float
    ci_low: float | None = None
    ci_high: float | None = None
    n: int | None = None


class Series(_Model):
    name: str
    points: list[Point]


class ChartDataset(_Model):
    chart_id: str
    title: str
    x_label: str
    y_label: str
    x_scale: Literal["linear", "log", "category"] = "linear"
    y_scale: Literal["linear", "log"] = "linear"
    series: list[Series]
    notes: str | None = None
    target: Literal["real", "mock"]
    synthetic: bool
    experiment_id: str | None = None
    run_ids: list[str] = Field(default_factory=list)

    def to_json(self) -> dict:
        """Wire form: absent optional keys are omitted, never null (D's zod treats them as optional, not nullable)."""
        return self.model_dump(mode="json", exclude_none=True)
