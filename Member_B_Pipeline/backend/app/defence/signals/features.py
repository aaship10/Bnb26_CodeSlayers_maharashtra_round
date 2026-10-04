"""Signal functions: pure, deterministic, each returns a value in [0, 1] plus a small explanation.

0 = nothing unusual, 1 = as suspicious as this signal can say. The risk engine multiplies by the
configured per-signal weight (the MOST a signal can contribute), so a single weak heuristic can never
decide an outcome alone. Every threshold below is a starting point to be tuned with Member C's data and
is documented in docs/DEFENCES.md together with its false-positive behaviour.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# These four describe the NETWORK, not the person: behind a campus NAT thousands of real students share
# an address, and on orientation day a whole hostel legitimately registers within minutes (which looks
# exactly like a bot burst from one subnet). The risk engine caps their COMBINED contribution below the
# challenge threshold, so network evidence alone can never challenge or down-weight anyone.
IP_ONLY_SIGNALS = frozenset({"accounts_per_ip", "accounts_per_subnet", "accounts_per_asn", "registration_velocity"})


@dataclass(frozen=True)
class Signal:
    name: str
    value: float  # [0, 1]
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.value <= 1.0:
            raise ValueError(f"signal {self.name} value {self.value} outside [0, 1]")


def clamp01(x: float) -> float:
    return 0.0 if x <= 0 else 1.0 if x >= 1 else x


def log_scaled(n: int, full_at: int) -> float:
    """0 for a lone account, 1 at `full_at` accounts, logarithmic in between (10 accounts is much more
    common and much less suspicious than 1000)."""
    return 0.0 if n <= 1 else clamp01(math.log10(n) / math.log10(full_at))


# ------------------------------------------------------------------------------ request-derived
_AUTOMATION_UA = ("python-requests", "python-httpx", "aiohttp", "curl/", "wget/", "go-http-client", "okhttp", "libwww-perl",
                  "node-fetch", "axios/", "scrapy", "java/", "httpclient", "postmanruntime", "insomnia")


def header_anomaly(headers: Mapping[str, str]) -> Signal:
    """Missing or tool-like User-Agent, headless browser, missing Accept-Language. Real browsers always
    send all of these; scripts often do not. Trivially faked by a careful bot, so it only catches lazy ones."""
    ua = (headers.get("user-agent") or "").strip().lower()
    if not ua:
        return Signal("header_anomaly", 1.0, {"reason": "no_user_agent"})
    if any(tok in ua for tok in _AUTOMATION_UA):
        return Signal("header_anomaly", 0.9, {"reason": "automation_user_agent"})
    if "headless" in ua:
        return Signal("header_anomaly", 0.6, {"reason": "headless_browser"})
    if not (headers.get("accept-language") or "").strip():
        return Signal("header_anomaly", 0.4, {"reason": "no_accept_language"})
    return Signal("header_anomaly", 0.0)


def timing_regularity(intervals_ms: Sequence[float], min_samples: int) -> Signal:
    """Coefficient of variation of the gaps between one identity's /enter and /challenge requests.
    People are irregular (CV well above 0.3); a loop with a fixed sleep is not (CV near 0).
    Needs `min_samples` intervals before it says anything. NOT applied to /status polling: the frontend's
    own polling is deliberately periodic, and flagging it would flag every honest user."""
    if len(intervals_ms) < min_samples:
        return Signal("timing_regularity", 0.0, {"samples": len(intervals_ms), "needed": min_samples})
    mean = statistics.fmean(intervals_ms)
    if mean <= 0:
        return Signal("timing_regularity", 1.0, {"reason": "zero_gaps", "samples": len(intervals_ms)})
    cv = statistics.pstdev(intervals_ms) / mean
    return Signal("timing_regularity", clamp01(1.0 - cv / 0.30), {"cv": round(cv, 3), "samples": len(intervals_ms)})


# ----------------------------------------------------------------------------- identity-derived
def accounts_per_device(n: int) -> Signal:
    # siblings or a lab PC: 2-3 accounts are ordinary (0.1-0.2); 11+ saturates.
    return Signal("accounts_per_device", clamp01((n - 1) / 10), {"accounts": n})


def accounts_per_ip(n: int) -> Signal:
    return Signal("accounts_per_ip", log_scaled(n, 1000), {"accounts": n})


def accounts_per_subnet(n: int) -> Signal:
    return Signal("accounts_per_subnet", log_scaled(n, 2000), {"accounts": n})


def accounts_per_asn(n: int, asn: int | None = None) -> Signal:
    return Signal("accounts_per_asn", log_scaled(n, 20000), {"accounts": n, "asn": asn})


def account_age(age_s: float) -> Signal:
    """Accounts created minutes before the drop are riskier than ones created days ago. Linear down to
    zero at six hours. Legitimate late registrants exist, hence the small weight."""
    age_s = max(0.0, age_s)
    return Signal("account_age", clamp01(1.0 - age_s / 21600), {"age_s": int(age_s)})


def registration_velocity(burst: int) -> Signal:
    """Other accounts verified from the same /24 in the same ten minutes. 20 is background noise (a
    hostel on orientation day); 200+ is a script."""
    return Signal("registration_velocity", clamp01((burst - 20) / 180), {"same_subnet_same_10min": burst})


def email_pattern(flags: Mapping[str, Any]) -> Signal:
    hit = bool(flags.get("sequential_pattern"))
    return Signal("email_pattern", 1.0 if hit else 0.0, {"cluster_size": flags.get("pattern_cluster_size", 0)} if hit else {})


def email_entropy(flags: Mapping[str, Any]) -> Signal:
    return Signal("email_entropy", 1.0 if flags.get("random_local_part") else 0.0)


def otp_latency(ms: int | None) -> Signal:
    """Time between asking for the code and entering it. Reading an email and typing six digits takes a
    person several seconds; a script is instant. 2 s or less is full score, 8 s or more none."""
    if ms is None:
        return Signal("otp_latency", 0.0)
    return Signal("otp_latency", clamp01((8000 - ms) / 6000), {"ms": ms})
