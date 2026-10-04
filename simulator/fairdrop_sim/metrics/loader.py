"""Load a run directory (written by engine/run.py) into memory for the metrics engine."""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from fairdrop_sim.engine.recorder import Recorder


@dataclass
class RunData:
    run_dir: Path
    meta: dict
    users: pd.DataFrame
    recorder: Recorder
    decisions: list[dict]


def load_run(run_dir: str | Path) -> RunData:
    d = Path(run_dir)
    meta = json.loads((d / "run.json").read_text(encoding="utf-8"))
    with gzip.open(d / "users.csv.gz", "rt", encoding="utf-8") as f:
        users = pd.read_csv(f)
    for col in ("srv_state", "srv_draw_state", "srv_weight", "srv_arrival_seq", "srv_client_ip"):
        if col not in users.columns:
            users[col] = pd.NA
    rec_dict = json.loads((d / "recorder.json").read_text(encoding="utf-8"))
    rec = Recorder.merged([rec_dict], rec_dict.get("t0", 0.0))
    decisions = json.loads((d / "decisions.json").read_text(encoding="utf-8")) if (d / "decisions.json").exists() else []
    return RunData(d, meta, users, rec, decisions)
