"""Pydantic request/response models: the API contract consumed by the frontend.

All datetimes are serialised in UTC (ISO 8601 with +00:00).
"""
from __future__ import annotations

from datetime import datetime, timezone
try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.services.events import EventInfo


def _utc(v: datetime | None) -> datetime | None:
    return v.astimezone(UTC) if v is not None else None


UtcDatetime = Annotated[datetime, AfterValidator(_utc)]

Mode = Literal["LOTTERY", "FCFS"]
Phase = Literal["DRAFT", "SCHEDULED", "OPEN", "DRAWING", "CLAIMING", "CLOSED"]
WindowStatus = Literal["BEFORE", "OPEN", "CLOSED"]
EntryState = Literal["REGISTERED", "ENTERED", "WON", "WAITLISTED", "CLAIMED", "EXPIRED", "LOST"]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


# ------------------------------------------------------------------ auth
class DevLoginRequest(BaseModel):
    # Plain pattern rather than EmailStr: email-validator rejects the .test TLD used by seeds.
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    display_name: str | None = Field(default=None, max_length=100)


class DevLoginResponse(BaseModel):
    token: str
    user_id: UUID


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    display_name: str = Field(default="", max_length=100)
    hp: str = Field(default="")


class VerifyRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    otp: str = Field(min_length=1, max_length=20)


class SessionResponse(BaseModel):
    token: str
    expires_at: UtcDatetime
    user_id: UUID


class UserMeResponse(BaseModel):
    user_id: UUID
    email: str
    display_name: str
    is_admin: bool = False


# ------------------------------------------------------------------ events (public)
class EventPublic(BaseModel):
    """Public event view. Never contains secrets, weights or per-entrant data."""
    id: UUID
    name: str
    inventory: int
    mode: Mode
    phase: Phase
    window_status: WindowStatus
    window_opens_at: UtcDatetime
    window_closes_at: UtcDatetime
    claim_ttl_seconds: int
    claim_ttl_s: int
    claim_phase_ends_at: UtcDatetime | None
    seed_commitment: str | None
    beacon_round: int | None
    algorithm_version: str
    drawn_at: UtcDatetime | None
    server_now: UtcDatetime

    @classmethod
    def build(cls, ev: EventInfo, now: datetime) -> EventPublic:
        return cls(
            id=ev.id, name=ev.name, inventory=ev.inventory, mode=ev.mode, phase=ev.phase,
            window_status=ev.window_status(now), window_opens_at=ev.window_opens_at,
            window_closes_at=ev.window_closes_at, claim_ttl_seconds=ev.claim_ttl_seconds,
            claim_ttl_s=ev.claim_ttl_seconds,
            claim_phase_ends_at=ev.claim_phase_ends_at, seed_commitment=ev.seed_commitment,
            beacon_round=ev.beacon_round, algorithm_version=ev.algorithm_version,
            drawn_at=ev.drawn_at, server_now=now,
        )


class EventList(BaseModel):
    events: list[EventPublic]
    server_now: UtcDatetime


class EnterResponse(BaseModel):
    state: EntryState
    entered_at: UtcDatetime
    already_entered: bool
    server_now: UtcDatetime


class StatusResponse(BaseModel):
    """Pure re-read of server state; safe to poll after refresh/reconnect.

    Before the draw completes this only ever says REGISTERED or ENTERED: no
    rank, weight, public_id or waitlist information is exposed.
    """
    event_id: UUID
    phase: Phase
    window_status: WindowStatus
    state: EntryState
    entered_at: UtcDatetime | None = None
    public_id: UUID | None = None            # only after the draw
    hold_expires_at: UtcDatetime | None = None   # only while WON (holding a seat)
    waitlist_position: int | None = None     # only while WAITLISTED, after the draw
    seat_no: int | None = None               # only when CLAIMED
    ticket_code: str | None = None           # only when CLAIMED
    server_now: UtcDatetime


# ------------------------------------------------------------------ admin
class EventCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    inventory: int = Field(ge=1, le=1_000_000)
    window_opens_at: AwareDatetime
    window_closes_at: AwareDatetime
    claim_ttl_seconds: int = Field(default=300, ge=1, le=86_400)
    claim_ttl_s: int | None = Field(default=None, ge=1, le=86_400)
    claim_phase_seconds: int | None = Field(
        default=None, ge=1, le=30 * 86_400,
        description="Claim phase length after the window closes. Default: 3 x claim_ttl_seconds.")
    mode: Mode = "LOTTERY"
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> EventCreate:
        if self.claim_ttl_s is not None:
            self.claim_ttl_seconds = self.claim_ttl_s
        if self.window_closes_at <= self.window_opens_at:
            raise ValueError("window_closes_at must be after window_opens_at")
        if self.claim_phase_seconds is None:
            self.claim_phase_seconds = 3 * self.claim_ttl_seconds
        return self


class EventPatch(BaseModel):
    """PATCH /admin/events/{id}/config. `config` replaces the whole blob.
    mode and timings may only change while the event is DRAFT."""
    model_config = ConfigDict(extra="forbid")

    mode: Mode | None = None
    config: dict[str, Any] | None = None
    window_opens_at: AwareDatetime | None = None
    window_closes_at: AwareDatetime | None = None
    claim_ttl_seconds: int | None = Field(default=None, ge=1, le=86_400)
    claim_phase_seconds: int | None = Field(default=None, ge=1, le=30 * 86_400)


class EventAdmin(EventPublic):
    """Admin view: public fields plus operational ones. Still never the server seed
    before reveal (it lives in event_secrets, which this view does not read)."""
    claim_phase_seconds: int
    beacon_randomness: str | None
    entrants_hash: str | None
    entrant_count: int | None
    final_seed: str | None
    revealed_seed: str | None
    closed_at: UtcDatetime | None
    config: dict[str, Any]
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @classmethod
    def build(cls, ev: EventInfo, now: datetime) -> EventAdmin:  # type: ignore[override]
        base = EventPublic.build(ev, now).model_dump()
        config = dict(ev.config or {})
        if "defences" not in config:
            config["defences"] = {
                "preset": "rate_limit+pow",
                "layers": {
                    "rate_limit": {"enabled": True, "per_ip_rps": 5, "per_user_rps": 2, "burst": 10},
                    "pow": {"enabled": True, "difficulty_bits": 18},
                    "captcha": {"enabled": False, "provider": "mock", "when_risk_at_least": 0.7},
                    "signals": {"enabled": False, "device_id": True, "honeypot": True, "timing": True},
                    "risk": {"enabled": False, "challenge_at": 0.5, "reject_at": 0.9},
                },
            }
        return cls(
            **base, claim_phase_seconds=ev.claim_phase_seconds,
            beacon_randomness=ev.beacon_randomness, entrants_hash=ev.entrants_hash,
            entrant_count=ev.entrant_count, final_seed=ev.final_seed,
            revealed_seed=ev.revealed_seed, closed_at=ev.closed_at, config=config,
            created_at=ev.created_at, updated_at=ev.updated_at,
        )


class ClaimResponse(BaseModel):
    state: Literal["CLAIMED"] = "CLAIMED"
    seat_no: int
    ticket_code: str
    server_now: UtcDatetime


# ------------------------------------------------------------------ fairness (public)
class FairnessBeacon(BaseModel):
    source: str
    round: int
    randomness: str | None = None      # hex; known once the round is published and fetched


class FairnessOutcome(BaseModel):
    winners_count: int
    waitlist_count: int
    winners_hash: str
    waitlist_hash: str


class FairnessResponse(BaseModel):
    """Everything needed to re-run the draw. The server seed, final seed and
    result appear only after the draw; the commitment is public from scheduling."""
    event_id: UUID
    phase: Phase
    algorithm_version: str
    seed_commitment: str
    server_seed: str | None
    beacon: FairnessBeacon | None
    entrants_hash: str | None
    entrants_count: int | None
    final_seed: str | None
    inventory: int
    result: FairnessOutcome | None
    audit_head_hash: str | None
    server_now: UtcDatetime


class FairnessEntrant(BaseModel):
    public_id: str
    weight: float


class FairnessEntrants(BaseModel):
    event_id: UUID
    count: int
    entrants: list[FairnessEntrant]


class FairnessResults(BaseModel):
    event_id: UUID
    winners: list[str]
    waitlist: list[str]


class TransitionResult(BaseModel):
    event: EventAdmin
    changed: bool = Field(description="false if the event was already past this transition (idempotent no-op)")
