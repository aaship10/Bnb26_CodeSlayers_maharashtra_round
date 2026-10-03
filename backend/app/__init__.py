"""Fair Drop core allocation engine (Member A)."""
import asyncio
import sys

if sys.platform == "win32":
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop.
    # Selector loop is required for local development on Windows; Linux
    # deployments are unaffected.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
