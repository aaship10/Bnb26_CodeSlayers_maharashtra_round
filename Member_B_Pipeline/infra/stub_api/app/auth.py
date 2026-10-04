"""The single get_current_user dependency.

Stage 2: the dev stub (trust X-User-Id) was replaced by Member B's implementation. That
one-line swap is exactly what A does in the real backend. AUTH_MODE=dev keeps the
X-User-Id behaviour (without a DB lookup); AUTH_MODE=jwt requires a bearer token.
"""
from app.defence.identity.deps import get_current_user  # noqa: F401
