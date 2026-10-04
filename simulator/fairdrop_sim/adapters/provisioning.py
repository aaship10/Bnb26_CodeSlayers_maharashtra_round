"""Make sure every simulated identity exists on the target before the window opens.

  mock : nothing to do - the mock accepts any well-formed user id (it auto-registers).
  real : Stage 7 - bulk INSERT into users (with sim_label, read only by C) via a
         provisioning role, deterministic ids from the population seed; plus a small
         `register_sample` through the real /auth/register + Mailpit OTP flow.

Tokens for AUTH_MODE=jwt are minted per shard via B's POST /admin/sim/tokens
(see engine/shard.py), so no provisioning step is needed for them.
"""
from __future__ import annotations

from fairdrop_sim.crowd.population import Humans


async def provision(target: str, humans: Humans, register_sample: int = 0) -> dict[str, int]:
    if target == "mock":
        return {"provisioned": len(humans), "registered_via_flow": 0}
    raise NotImplementedError(
        "Real-target provisioning arrives in Stage 7 (needs A's users schema and a write role for "
        "sim users; see docs/INTERFACE_REQUESTS_C.md A-C6/A-C7)"
    )
