"""The two reference solvers handed to C (Python) and D (JavaScript) must agree with the spec vectors
and with the backend's own check. They are standalone scripts, so they are tested as users run them."""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from app.defence.pow import protocol

ROOT = Path(__file__).resolve().parents[2]
VECTORS = json.loads((ROOT / "docs" / "pow_vectors.json").read_text(encoding="utf-8"))["rule_vectors"]
QUICK = [v for v in VECTORS if v["bits"] <= 12]  # keep the subprocess runs short


def load_py():
    spec = importlib.util.spec_from_file_location("pow_solver", ROOT / "scripts" / "pow_solver.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("v", VECTORS, ids=lambda v: f"{v['prefix'][:10]}-{v['bits']}")
def test_python_reference_matches_vectors(v):
    assert load_py().solve(v["prefix"], v["bits"]) == v["nonce"]


def test_python_reference_cli():
    v = next(x for x in VECTORS if x["bits"] == 12 and x["prefix"] == "vector-a")
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "pow_solver.py"), v["prefix"], "12"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and f"nonce={v['nonce']}" in out.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("v", QUICK, ids=lambda v: f"{v['prefix'][:10]}-{v['bits']}")
def test_javascript_reference_matches_vectors(v):
    out = subprocess.run(["node", str(ROOT / "scripts" / "pow_solver.mjs"), v["prefix"], str(v["bits"])], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and f"nonce={v['nonce']}" in out.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_javascript_and_python_agree_on_a_real_challenge_and_the_backend_accepts_it():
    import uuid

    tok = protocol.mint("k" * 40, "p", uuid.UUID(int=1), uuid.UUID(int=2), 2_000_000_000, 14)
    js = subprocess.run(["node", str(ROOT / "scripts" / "pow_solver.mjs"), tok, "14"], capture_output=True, text=True, timeout=60).stdout
    nonce = js.split("nonce=")[1].split()[0]
    assert nonce == load_py().solve(tok, 14) == protocol.solve(tok, 14)
    assert protocol.solution_ok(tok, nonce, 14)
