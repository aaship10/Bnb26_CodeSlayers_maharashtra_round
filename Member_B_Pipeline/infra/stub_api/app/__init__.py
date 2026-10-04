"""STUB of Member A's backend. When A's backend lands, run it instead (the runner takes
BACKEND_DIR=backend); nothing here needs deleting.

Local (no-Docker) wiring: the defence package lives in backend/app/defence. Instead of
copying it into this directory, graft backend/app onto this package's search path so
`import app.defence` resolves to it.
"""
from pathlib import Path

_defence_parent = Path(__file__).resolve().parents[3] / "backend" / "app"
if (_defence_parent / "defence").is_dir() and str(_defence_parent) not in __path__:
    __path__.append(str(_defence_parent))
