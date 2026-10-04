"""In-memory Fair Drop double: event lifecycle, entries, draw, holds, claims, invariants.

DEVELOPMENT DOUBLE ONLY. Results produced against it are marked target="mock".
It mirrors the shared contract (and the error statuses D's mock uses) closely
enough that client code paths are exercised, but it is not A's engine.

Pure domain logic, no HTTP. Every mutating method takes `now` (epoch seconds)
so tests drive time explicitly. Not thread-safe: the app runs it on one event
loop with no awaits inside a mutation, which makes each call atomic.
"""
from __future__ import annotations

import hashlib
import hmac
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from heapq import heappop, heappush
from typing import Any

from .defences import DefenceState, expand_defences

PHASES = ("DRAFT", "SCHEDULED", "OPEN", "DRAWING", "CLAIMING", "CLOSED")


class MockError(Exception):
    """Maps onto the contract error body {code, message, details?}."""

    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers or {}


def iso(t: float | None) -> str | None:
    if t is None:
        return None
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def seed_commitment(seed_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(seed_hex)).hexdigest()


def draw_key(seed_hex: str, event_id: str, user_id: str, weight: float) -> float:
    """Weighted sampling without replacement (Efraimidis-Spirakis, exponential form).

    u ~ U(0,1) is derived from HMAC(seed, event:user), so the outcome depends only
    on the seed and the SET of (user, weight) pairs, never on arrival order.
    Smallest key wins. P(win first slot) is proportional to weight."""
    mac = hmac.new(bytes.fromhex(seed_hex), f"{event_id}:{user_id}".encode(), hashlib.sha256).digest()
    u = (int.from_bytes(mac[:8], "big") + 0.5) / 2**64
    return -math.log(u) / weight


@dataclass
class Entry:
    user_id: str
    arrival_seq: int
    entered_at: float
    client_ip: str
    device_id: str | None
    state: str = "ENTERED"  # ENTERED | WON | WAITLISTED | LOST | CLAIMED | EXPIRED
    weight: float = 1.0
    risk: float = 0.0
    draw_state: str | None = None  # state right after the draw (lottery) or at entry (FCFS)
    queue_index: int | None = None  # position in the ordered queue (draw order or FCFS arrival)
    promoted: bool = False
    hold_expires_at: float | None = None
    seat_no: int | None = None
    ticket_code: str | None = None
    claimed_at: float | None = None


@dataclass
class Event:
    id: str
    name: str
    inventory: int
    mode: str
    window_seconds: float
    claim_ttl_s: float
    server_seed_hex: str
    defences: dict[str, Any]
    description: str = ""
    phase: str = "DRAFT"
    opens_at: float | None = None
    closes_at: float | None = None
    drawn_at: float | None = None
    entries: dict[str, Entry] = field(default_factory=dict)
    entry_log: list[str] = field(default_factory=list)  # append-only user ids, for duplicate checks
    queue: list[str] = field(default_factory=list)  # ordered: lottery = draw order, FCFS = arrival
    queue_head: int = 0  # next queue index to promote from
    holds: list[tuple[float, str]] = field(default_factory=list)  # heap (expires_at, user_id)
    held: int = 0
    confirmed: int = 0
    seq: int = 0
    idempotency: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    defence_state: DefenceState = field(default_factory=DefenceState)

    @property
    def commitment(self) -> str:
        return seed_commitment(self.server_seed_hex)


class Store:
    def __init__(self) -> None:
        self.events: dict[str, Event] = {}
        self._counter = 0

    # ------------------------------------------------------------------ admin

    def create_event(self, now: float, *, name: str = "Mock drop", inventory: int = 500, mode: str = "LOTTERY",
                     window_seconds: float = 60, claim_ttl_seconds: float = 60, event_id: str | None = None,
                     server_seed_hex: str | None = None, defences: dict[str, Any] | None = None,
                     description: str = "") -> Event:
        if mode not in ("LOTTERY", "FCFS"):
            raise MockError(400, "VALIDATION_ERROR", "mode must be LOTTERY or FCFS", {"field": "mode"})
        if inventory < 1:
            raise MockError(400, "VALIDATION_ERROR", "inventory must be >= 1", {"field": "inventory"})
        if event_id is None:
            self._counter += 1
            event_id = f"evt_mock_{self._counter:04d}"
        if event_id in self.events:
            raise MockError(400, "VALIDATION_ERROR", f"event {event_id} already exists", {"field": "id"})
        seed = server_seed_hex or hashlib.sha256(f"mock-seed/{event_id}".encode()).hexdigest()
        _check_seed(seed)
        ev = Event(id=event_id, name=name, inventory=inventory, mode=mode, window_seconds=window_seconds,
                   claim_ttl_s=claim_ttl_seconds, server_seed_hex=seed,
                   defences=expand_defences(defences or {"preset": "none"}), description=description)
        self.events[event_id] = ev
        return ev

    def get(self, event_id: str) -> Event:
        ev = self.events.get(event_id)
        if ev is None:
            raise MockError(404, "NOT_FOUND", f"No such event: {event_id}")
        return ev

    def schedule(self, ev: Event, now: float, opens_at: float | None, window_seconds: float | None) -> None:
        if ev.phase not in ("DRAFT", "SCHEDULED"):
            raise MockError(409, "VALIDATION_ERROR", f"cannot schedule in phase {ev.phase}")
        if window_seconds is not None:
            ev.window_seconds = window_seconds
        ev.opens_at = opens_at if opens_at is not None else now
        ev.closes_at = ev.opens_at + ev.window_seconds
        ev.phase = "SCHEDULED"
        self.tick(ev, now)

    def open(self, ev: Event, now: float) -> None:
        if ev.phase not in ("DRAFT", "SCHEDULED"):
            raise MockError(409, "VALIDATION_ERROR", f"cannot open in phase {ev.phase}")
        ev.opens_at = now
        ev.closes_at = now + ev.window_seconds
        ev.phase = "OPEN"

    def close(self, ev: Event, now: float) -> None:
        self.tick(ev, now)
        if ev.phase != "OPEN":
            raise MockError(409, "VALIDATION_ERROR", f"cannot close in phase {ev.phase}")
        ev.closes_at = now
        self.tick(ev, now)

    def reset(self, ev: Event, server_seed_hex: str | None) -> None:
        if server_seed_hex:
            _check_seed(server_seed_hex)
        fresh = Event(id=ev.id, name=ev.name, inventory=ev.inventory, mode=ev.mode,
                      window_seconds=ev.window_seconds, claim_ttl_s=ev.claim_ttl_s,
                      server_seed_hex=server_seed_hex or ev.server_seed_hex, defences=ev.defences,
                      description=ev.description)
        self.events[ev.id] = fresh

    def set_config(self, ev: Event, defences: dict[str, Any]) -> None:
        ev.defences = expand_defences(defences)

    # ------------------------------------------------------------------ time

    def tick(self, ev: Event, now: float) -> None:
        """Advance phases and expire holds lazily. Idempotent for a given `now`."""
        if ev.phase == "SCHEDULED" and ev.opens_at is not None and now >= ev.opens_at:
            ev.phase = "OPEN"
        if ev.phase == "OPEN" and ev.closes_at is not None and now >= ev.closes_at:
            ev.phase = "DRAWING" if ev.mode == "LOTTERY" else "CLAIMING"
        if ev.phase == "CLAIMING" or (ev.mode == "FCFS" and ev.phase == "OPEN"):
            self._expire_and_promote(ev, now)
        if ev.phase == "CLAIMING" and ev.held == 0 and (
            ev.queue_head >= len(ev.queue) or ev.confirmed >= ev.inventory
        ):
            ev.phase = "CLOSED"
            for uid in ev.queue[ev.queue_head:]:
                e = ev.entries[uid]
                if e.state == "WAITLISTED":
                    e.state = "LOST"

    def _expire_and_promote(self, ev: Event, now: float) -> None:
        # Freed seats go to the queue in strict order. A promoted hold starts at the
        # moment the previous hold expired (not when we noticed), so outcomes don't
        # depend on when requests happen to arrive.
        self._promote(ev, now)
        while ev.holds and ev.holds[0][0] <= now:
            expires, uid = heappop(ev.holds)
            e = ev.entries[uid]
            if e.state != "WON" or e.hold_expires_at != expires:
                continue  # claimed (or stale heap item)
            e.state = "EXPIRED"
            ev.held -= 1
            self._promote(ev, expires)

    def _promote(self, ev: Event, at: float) -> None:
        while ev.held + ev.confirmed < ev.inventory and ev.queue_head < len(ev.queue):
            uid = ev.queue[ev.queue_head]
            ev.queue_head += 1
            e = ev.entries[uid]
            if e.state not in ("WAITLISTED", "ENTERED"):
                continue
            e.promoted = e.state == "WAITLISTED"
            self._hold(ev, e, at)

    def _hold(self, ev: Event, e: Entry, at: float) -> None:
        e.state = "WON"
        e.hold_expires_at = at + ev.claim_ttl_s
        ev.held += 1
        heappush(ev.holds, (e.hold_expires_at, e.user_id))

    def tick_all(self, now: float) -> None:
        for ev in self.events.values():
            self.tick(ev, now)

    # ------------------------------------------------------------------ users

    def enter(self, ev: Event, user_id: str, now: float, client_ip: str, device_id: str | None) -> tuple[Entry, bool]:
        self.tick(ev, now)
        existing = ev.entries.get(user_id)
        if existing is not None:
            return existing, True  # idempotent: window checks don't apply to a replay
        if ev.phase in ("DRAFT", "SCHEDULED"):
            raise MockError(409, "WINDOW_NOT_OPEN", "The entry window has not opened")
        if ev.phase != "OPEN":
            raise MockError(409, "WINDOW_CLOSED", "The entry window has closed")
        ev.seq += 1
        e = Entry(user_id=user_id, arrival_seq=ev.seq, entered_at=now, client_ip=client_ip, device_id=device_id)
        ev.entries[user_id] = e
        ev.entry_log.append(user_id)
        if ev.mode == "FCFS":
            e.queue_index = len(ev.queue)
            ev.queue.append(user_id)
            self._promote(ev, now)
            if e.state == "ENTERED":
                e.state = "WAITLISTED"
            e.draw_state = e.state
        return e, False

    def draw(self, ev: Event, now: float, server_seed_hex: str | None = None) -> dict[str, Any]:
        self.tick(ev, now)
        if ev.mode != "LOTTERY":
            raise MockError(409, "VALIDATION_ERROR", "FCFS events have no draw")
        if ev.phase != "DRAWING":
            raise MockError(409, "VALIDATION_ERROR", f"cannot draw in phase {ev.phase} (close the window first)")
        if server_seed_hex:
            _check_seed(server_seed_hex)
            ev.server_seed_hex = server_seed_hex
        ev.defence_state.apply_risk(ev, now)  # sets entry weights if the risk layer is on
        order = draw_order(ev.server_seed_hex, ev.id, [(e.user_id, e.weight) for e in ev.entries.values()])
        ev.queue = order
        for i, uid in enumerate(order):
            e = ev.entries[uid]
            e.queue_index = i
        ev.drawn_at = now
        ev.phase = "CLAIMING"
        self._promote(ev, now)  # first `inventory` in draw order get holds
        for e in ev.entries.values():
            if e.weight <= 0:
                e.state = "LOST"
            elif e.state == "ENTERED":
                e.state = "WAITLISTED"
            e.draw_state = e.state
        self.tick(ev, now)
        return {"winners": min(ev.inventory, len(order)), "entries": len(ev.entries), "waitlisted": max(0, len(order) - ev.inventory)}

    def status(self, ev: Event, user_id: str, now: float) -> dict[str, Any]:
        self.tick(ev, now)
        e = ev.entries.get(user_id)
        out: dict[str, Any] = {"phase": ev.phase}
        if e is None:
            out["state"] = "REGISTERED"
            return out
        out["state"] = e.state
        drawn = ev.mode == "FCFS" or ev.drawn_at is not None
        if not drawn:
            out["state"] = "ENTERED"  # no rank fields before the draw (D's A7)
            return out
        out["public_id"] = public_id(ev.id, user_id)
        if e.state == "WON":
            out["hold_expires_at"] = iso(e.hold_expires_at)
        elif e.state == "WAITLISTED" and e.queue_index is not None:
            out["waitlist_position"] = e.queue_index - ev.queue_head + 1
        elif e.state == "CLAIMED":
            out["seat_no"] = e.seat_no
            out["ticket_code"] = e.ticket_code
        return out

    def claim(self, ev: Event, user_id: str, key: str, now: float) -> dict[str, Any]:
        if len(key) < 8:
            raise MockError(400, "VALIDATION_ERROR", "Idempotency-Key header is required (min 8 chars)",
                            {"field": "Idempotency-Key"})
        self.tick(ev, now)
        replay = ev.idempotency.get((user_id, key))
        if replay is not None:
            return replay
        e = ev.entries.get(user_id)
        if e is None or e.state in ("ENTERED", "WAITLISTED", "LOST"):
            raise MockError(403, "NOT_WINNER", "No seat is being held for you")
        if e.state == "CLAIMED":
            raise MockError(409, "ALREADY_CLAIMED", "You already claimed a seat")
        if e.state == "EXPIRED":
            raise MockError(410, "HOLD_EXPIRED", "Your hold has expired")
        assert e.state == "WON"
        ev.held -= 1
        ev.confirmed += 1
        e.state = "CLAIMED"
        e.seat_no = ev.confirmed
        e.claimed_at = now
        e.ticket_code = ticket_code(ev.id, user_id, e.seat_no)
        body = {"state": "CLAIMED", "seat_no": e.seat_no, "ticket_code": e.ticket_code}
        ev.idempotency[(user_id, key)] = body
        self.tick(ev, now)
        return body

    # ------------------------------------------------------------------ checks

    def invariants(self, ev: Event, now: float) -> dict[str, Any]:
        """Recomputed from entries, independent of the running counters."""
        self.tick(ev, now)
        states = Counter(e.state for e in ev.entries.values())
        occupied = states["WON"] + states["CLAIMED"]
        seats = [e.seat_no for e in ev.entries.values() if e.state == "CLAIMED"]
        log_counts = Counter(ev.entry_log)
        orphaned = sum(
            1 for e in ev.entries.values()
            if e.state == "WON" and (e.hold_expires_at is None or e.hold_expires_at <= now or ev.phase == "CLOSED")
        )
        checks = {
            "oversold": max(0, occupied - ev.inventory),
            "duplicate_users": sum(c - 1 for c in log_counts.values() if c > 1),
            "duplicate_seats": len(seats) - len(set(seats)) + sum(1 for s in seats if s is None or not 1 <= s <= ev.inventory),
            "orphaned_holds": orphaned,
            "counter_mismatch": int(ev.held != states["WON"]) + int(ev.confirmed != states["CLAIMED"]),
        }
        return {"passed": all(v == 0 for v in checks.values()), "checks": checks}

    def verify_draw(self, ev: Event) -> dict[str, Any]:
        """Recompute the draw order from the revealed seed and the entry set."""
        if ev.mode != "LOTTERY" or ev.drawn_at is None:
            return {"verified": None, "reason": "no draw to verify"}
        order = draw_order(ev.server_seed_hex, ev.id, [(e.user_id, e.weight) for e in ev.entries.values()])
        return {
            "verified": order == ev.queue,
            "seed_commitment": ev.commitment,
            "server_seed_hex": ev.server_seed_hex,
            "entries": len(ev.entries),
        }


def draw_order(seed_hex: str, event_id: str, entrants: list[tuple[str, float]]) -> list[str]:
    keyed = [(draw_key(seed_hex, event_id, uid, w), uid) for uid, w in entrants if w > 0]
    keyed.sort()
    return [uid for _, uid in keyed]


def public_id(event_id: str, user_id: str) -> str:
    return "p_" + hashlib.sha256(f"public|{event_id}|{user_id}".encode()).hexdigest()[:12]


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ticket_code(event_id: str, user_id: str, seat_no: int) -> str:
    h = hashlib.sha256(f"ticket|{event_id}|{user_id}|{seat_no}".encode()).digest()
    s = "".join(_CROCKFORD[b % 32] for b in h[:8])
    return f"FD-{s[:4]}-{s[4:]}"


def _check_seed(seed_hex: str) -> None:
    try:
        b = bytes.fromhex(seed_hex)
    except ValueError:
        b = b""
    if len(b) < 16:
        raise MockError(400, "VALIDATION_ERROR", "server_seed_hex must be at least 16 bytes of hex",
                        {"field": "server_seed_hex"})
