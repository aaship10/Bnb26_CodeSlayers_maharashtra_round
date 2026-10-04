"""Rate limits for the unauthenticated identity endpoints (deployment-level, not per event).

Starting points, to be tuned with Member C's data. Per-IP and per-subnet are generous on
purpose: orientation day puts a whole campus behind one NAT address. The tight buckets
are per device and per email, which a legitimate student does not exhaust.

OTP guessing bound: each code allows 5 attempts and a new code needs a /register, which is
limited to 3 burst + 1 per 5 min per email, so an attacker gets on the order of 15-20
guesses per hour per mailbox out of 10^6 (about 2e-5 success probability).
"""
from __future__ import annotations

from ..config_schema import Bucket
from ..ratelimit.bucket import Limit, hash_id

REGISTER: dict[str, Bucket] = {
    "ip": Bucket(capacity=200, refill_per_s=1.0),
    "subnet": Bucket(capacity=1000, refill_per_s=5.0),
    "device": Bucket(capacity=5, refill_per_s=1 / 120),
    "email": Bucket(capacity=3, refill_per_s=1 / 300),
}
VERIFY: dict[str, Bucket] = {
    "ip": Bucket(capacity=600, refill_per_s=10.0),
    "subnet": Bucket(capacity=3000, refill_per_s=50.0),
    "device": Bucket(capacity=20, refill_per_s=0.2),
    "email": Bucket(capacity=10, refill_per_s=1 / 6),
}


def limits_for(kind: str, buckets: dict[str, Bucket], ip: str, subnet: str, device_id: str | None, email: str) -> list[Limit]:
    ids = {"ip": ip, "subnet": subnet, "device": device_id, "email": hash_id(email)}
    out = []
    for scope, bucket in buckets.items():
        ident = ids[scope]
        if ident:  # no device id sent -> no device bucket (a missing id is a signal for stage 5, not a limit)
            out.append(Limit(scope, f"{kind}:{scope}:{ident}", bucket.capacity, bucket.refill_per_s))
    return out
