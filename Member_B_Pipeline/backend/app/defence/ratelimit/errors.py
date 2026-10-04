"""The 429 contract: RATE_LIMITED, Retry-After header, details {retry_after_ms, scope}."""
from __future__ import annotations

import math
import random

from ..errors import ApiError, ErrorCode
from .bucket import LimitResult


def rate_limited(result: LimitResult, jitter_max: float = 0.25, rng: random.Random | None = None) -> ApiError:
    """Build the 429. The wait is stretched by a random 0..jitter_max fraction so that
    a crowd rejected in the same instant does not retry in the same instant.
    The Retry-After header is whole seconds (HTTP), details.retry_after_ms is precise."""
    if result.allowed:
        raise ValueError("cannot build a 429 from an allowed result")
    r = rng or random
    retry_ms = max(1, math.ceil(result.retry_after_ms * (1 + r.uniform(0, jitter_max))))
    return ApiError(
        ErrorCode.RATE_LIMITED,
        "too many requests",
        details={"retry_after_ms": retry_ms, "scope": result.scope},
        headers={"Retry-After": str(max(1, math.ceil(retry_ms / 1000)))},
    )
