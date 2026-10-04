"""Fair Drop core allocation engine (Member A)."""
import asyncio
import sys

if sys.platform == "win32":
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop.
    # Selector loop is required for local development on Windows; Linux
    # deployments are unaffected.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Member B's defence package lives beside the core in Member_B_Pipeline/backend/app. Graft that
# directory onto this package's search path so `import app.defence` resolves to it without
# copying files (a real deployment may copy it into this directory instead; both work).
from pathlib import Path as _Path

_defence_parent = _Path(__file__).resolve().parents[2] / "Member_B_Pipeline" / "backend" / "app"
if (_defence_parent / "defence").is_dir() and str(_defence_parent) not in __path__:
    __path__.append(str(_defence_parent))
