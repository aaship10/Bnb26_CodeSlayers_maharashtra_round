"""Cross-language contract: everything the simulator writes for D must pass D's own zod schemas.

Skipped when the frontend's node_modules are not installed (run `npm install` in frontend/).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fairdrop_sim.samples import write_samples

REPO = Path(__file__).resolve().parents[2]
CHECK = Path(__file__).parent / "contract" / "frontend_schemas.check.ts"
TSX = REPO / "frontend" / "node_modules" / ".bin" / ("tsx.cmd" if sys.platform == "win32" else "tsx")


@pytest.mark.skipif(not TSX.exists() or shutil.which("node") is None, reason="frontend node_modules not installed")
def test_samples_pass_frontend_zod_schemas(tmp_path):
    write_samples(tmp_path)
    r = subprocess.run([str(TSX), str(CHECK), str(tmp_path)], capture_output=True, text=True, cwd=REPO / "frontend",
                       timeout=120, shell=sys.platform == "win32")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("OK ") >= 17
