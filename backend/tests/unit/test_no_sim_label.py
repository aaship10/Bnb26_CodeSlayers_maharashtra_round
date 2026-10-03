"""sim_label is simulation ground truth for Member C's analytics only.

No engine code (entry, draw, claim, sweeper, hooks, API) may read it, otherwise
the fairness evidence would be meaningless. This test fails if the identifier
appears anywhere under app/. Seed/simulation scripts and tests are allowed.
"""
import pathlib

APP_DIR = pathlib.Path(__file__).resolve().parents[2] / "app"


def test_app_code_never_references_sim_label():
    offenders = [
        str(p.relative_to(APP_DIR.parent))
        for p in APP_DIR.rglob("*.py")
        if "sim_label" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"sim_label referenced in engine code: {offenders}"


def test_scan_actually_covers_app_files():
    assert any(APP_DIR.rglob("*.py")), "app/ not found; the guard above would pass vacuously"
