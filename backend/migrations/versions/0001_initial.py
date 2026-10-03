"""Initial core schema.

Integrity guarantees enforced here by the database itself, not only by app code:
  * one entry per (event, user)                       -> UNIQUE (event_id, user_id)
  * a seat is held/confirmed by at most one allocation -> partial unique index
  * an entry holds/confirms at most one seat           -> partial unique index
  * allocations reference seats/entries of the SAME event -> composite FKs
  * entry and allocation states follow their state machines -> guard triggers
  * draw results (rank, waitlist position, weight after the draw) are immutable
  * the seed can only be revealed after the draw       -> CHECK on events
  * the audit log is append-only                       -> guard triggers
  * all deadlines use fd_now(), a single DB-side clock (dev offset supported)

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-03
"""
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

HEX64 = "'^[0-9a-f]{64}$'"

UPGRADE_SQL = f"""
-- ---------------------------------------------------------------- clock
-- One-row table holding a clock offset. Always 0 in prod; tests and demos move
-- it to "fast-forward" time for every replica at once.
CREATE TABLE dev_clock (
    id             boolean PRIMARY KEY DEFAULT true CHECK (id),
    offset_seconds double precision NOT NULL DEFAULT 0
);
INSERT INTO dev_clock DEFAULT VALUES;

-- STABLE + now() => one consistent instant per transaction, and the planner can
-- still use indexes for predicates such as hold_expires_at <= fd_now().
CREATE FUNCTION fd_now() RETURNS timestamptz
LANGUAGE sql STABLE AS $$
    SELECT now() + make_interval(
        secs => COALESCE((SELECT offset_seconds FROM dev_clock WHERE id), 0))
$$;

-- ---------------------------------------------------------------- users
-- B inserts (id, email, display_name) on verified registration. Emails must be
-- normalised (lower-cased, trimmed) by the inserter.
-- sim_label is simulation ground truth for Member C's analytics ONLY; no
-- allocation code may read it (enforced by tests/unit/test_no_sim_label.py).
CREATE TABLE users (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email        text NOT NULL UNIQUE CHECK (length(email) BETWEEN 3 AND 320),
    display_name text NOT NULL DEFAULT '',
    is_admin     boolean NOT NULL DEFAULT false,
    sim_label    text NULL CHECK (sim_label IN ('human', 'bot')),
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- events
CREATE TABLE events (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    inventory           integer NOT NULL CHECK (inventory BETWEEN 1 AND 1000000),
    mode                text NOT NULL DEFAULT 'LOTTERY' CHECK (mode IN ('LOTTERY', 'FCFS')),
    phase               text NOT NULL DEFAULT 'DRAFT'
                        CHECK (phase IN ('DRAFT', 'SCHEDULED', 'OPEN', 'DRAWING', 'CLAIMING', 'CLOSED')),
    window_opens_at     timestamptz NOT NULL,
    window_closes_at    timestamptz NOT NULL,
    claim_ttl_seconds   integer NOT NULL CHECK (claim_ttl_seconds > 0),
    -- Length of the claim phase after the window closes; claim_phase_ends_at is
    -- derived from it at scheduling time.
    claim_phase_seconds integer NOT NULL CHECK (claim_phase_seconds > 0),
    claim_phase_ends_at timestamptz NULL,
    seed_commitment     text NULL CHECK (seed_commitment ~ {HEX64}),
    beacon_round        bigint NULL CHECK (beacon_round > 0),
    beacon_randomness   text NULL CHECK (beacon_randomness ~ '^[0-9a-f]+$'),
    entrants_hash       text NULL CHECK (entrants_hash ~ {HEX64}),
    entrant_count       integer NULL CHECK (entrant_count >= 0),
    final_seed          text NULL CHECK (final_seed ~ {HEX64}),
    -- Public copy of the server seed, written only at reveal time. Public
    -- queries read this column and never touch event_secrets.
    revealed_seed       text NULL CHECK (revealed_seed ~ {HEX64}),
    algorithm_version   text NOT NULL DEFAULT 'fairdrop-draw-v1',
    drawn_at            timestamptz NULL,
    closed_at           timestamptz NULL,
    -- Opaque blob owned by Member B (defence toggles). Core code never interprets it.
    config              jsonb NOT NULL DEFAULT '{{}}' CHECK (jsonb_typeof(config) = 'object'),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CHECK (window_closes_at > window_opens_at),
    -- A lottery event past DRAFT must have committed to its seed and beacon round.
    CHECK (phase = 'DRAFT' OR mode = 'FCFS'
           OR (seed_commitment IS NOT NULL AND beacon_round IS NOT NULL)),
    -- The seed can never be revealed before the draw has produced final_seed.
    CHECK (revealed_seed IS NULL OR final_seed IS NOT NULL),
    CHECK (phase <> 'CLAIMING' OR mode = 'FCFS' OR final_seed IS NOT NULL)
);
CREATE INDEX events_phase_idx ON events (phase);

-- Secret seed in its own table so no public query can select it by accident.
CREATE TABLE event_secrets (
    event_id    uuid PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    server_seed text NOT NULL CHECK (server_seed ~ {HEX64}),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- seats
-- Exactly `inventory` rows per event, created at scheduling time. Overselling
-- is impossible because an active allocation needs a distinct seat row.
CREATE TABLE seats (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id uuid NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    seat_no  integer NOT NULL CHECK (seat_no >= 1),
    UNIQUE (event_id, seat_no),
    UNIQUE (id, event_id)          -- target of the composite FK from allocations
);

-- ---------------------------------------------------------------- entries
CREATE TABLE entries (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id          uuid NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id           uuid NOT NULL REFERENCES users(id),
    -- Pseudonymous id used in the draw and every public listing.
    public_id         uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    -- Arrival order, recorded only so Member C can show it does NOT predict winning.
    arrival_seq       bigserial,
    entered_at        timestamptz NOT NULL DEFAULT fd_now(),
    -- Draw weight in [0, 1]; 0 = excluded from the draw (see DRAW_SPEC.md).
    weight            numeric(5, 4) NOT NULL DEFAULT 1 CHECK (weight >= 0 AND weight <= 1),
    risk              jsonb NOT NULL DEFAULT '{{}}' CHECK (jsonb_typeof(risk) = 'object'),
    state             text NOT NULL DEFAULT 'ENTERED'
                      CHECK (state IN ('ENTERED', 'WON', 'WAITLISTED', 'CLAIMED', 'EXPIRED', 'LOST')),
    -- Overall position in the draw order (1..ranked entrants). Winners are
    -- ranks 1..inventory; waitlist_position = draw_rank - inventory for the rest.
    draw_rank         integer NULL CHECK (draw_rank >= 1),
    waitlist_position integer NULL CHECK (waitlist_position >= 1),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    UNIQUE (event_id, user_id),    -- one entry per identity, structurally
    UNIQUE (id, event_id),         -- target of the composite FK from allocations
    CHECK (waitlist_position IS NULL OR draw_rank IS NOT NULL),
    CHECK (state <> 'WAITLISTED' OR waitlist_position IS NOT NULL)
);
CREATE UNIQUE INDEX entries_rank_uq     ON entries (event_id, draw_rank)         WHERE draw_rank IS NOT NULL;
CREATE UNIQUE INDEX entries_waitlist_uq ON entries (event_id, waitlist_position) WHERE waitlist_position IS NOT NULL;
CREATE INDEX entries_event_state_idx    ON entries (event_id, state);
CREATE INDEX entries_event_public_idx   ON entries (event_id, public_id);

-- Entry state machine, enforced in the database so no code path (or manual
-- SQL) can move an entry backwards or skip states.
--   ENTERED    -> WON | WAITLISTED | LOST | CLAIMED (FCFS)
--   WAITLISTED -> WON (promotion) | LOST (event closed)
--   WON        -> CLAIMED | EXPIRED
--   CLAIMED, EXPIRED, LOST are terminal.
CREATE FUNCTION entries_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.event_id <> OLD.event_id OR NEW.user_id <> OLD.user_id
       OR NEW.public_id <> OLD.public_id OR NEW.arrival_seq <> OLD.arrival_seq
       OR NEW.entered_at <> OLD.entered_at THEN
        RAISE EXCEPTION 'entries: identity columns are immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.draw_rank IS NOT NULL AND NEW.draw_rank IS DISTINCT FROM OLD.draw_rank THEN
        RAISE EXCEPTION 'entries: draw_rank is immutable once set' USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.waitlist_position IS NOT NULL
       AND NEW.waitlist_position IS DISTINCT FROM OLD.waitlist_position THEN
        RAISE EXCEPTION 'entries: waitlist_position is immutable once set' USING ERRCODE = 'check_violation';
    END IF;
    -- The weight feeds entrants_hash and the draw; it may only change before the draw.
    IF NEW.weight <> OLD.weight AND (OLD.state <> 'ENTERED' OR OLD.draw_rank IS NOT NULL) THEN
        RAISE EXCEPTION 'entries: weight is frozen after the draw' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
           (OLD.state = 'ENTERED'    AND NEW.state IN ('WON', 'WAITLISTED', 'LOST', 'CLAIMED'))
        OR (OLD.state = 'WAITLISTED' AND NEW.state IN ('WON', 'LOST'))
        OR (OLD.state = 'WON'        AND NEW.state IN ('CLAIMED', 'EXPIRED'))
    ) THEN
        RAISE EXCEPTION 'entries: illegal state transition % -> %', OLD.state, NEW.state
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;
CREATE TRIGGER entries_guard BEFORE UPDATE ON entries
    FOR EACH ROW EXECUTE FUNCTION entries_guard();

-- ---------------------------------------------------------------- allocations
CREATE TABLE allocations (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id        uuid NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    entry_id        bigint NOT NULL,
    seat_id         bigint NOT NULL,
    status          text NOT NULL CHECK (status IN ('HELD', 'CONFIRMED', 'EXPIRED', 'RELEASED')),
    held_at         timestamptz NULL,
    hold_expires_at timestamptz NULL,
    confirmed_at    timestamptz NULL,
    ended_at        timestamptz NULL,     -- when it became EXPIRED / RELEASED
    ticket_code     text NULL UNIQUE,
    created_at      timestamptz NOT NULL DEFAULT fd_now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    -- Composite FKs: an allocation cannot pair a seat or entry from another event.
    FOREIGN KEY (entry_id, event_id) REFERENCES entries (id, event_id) ON DELETE CASCADE,
    FOREIGN KEY (seat_id, event_id)  REFERENCES seats (id, event_id)   ON DELETE CASCADE,
    CHECK (status <> 'HELD' OR (held_at IS NOT NULL AND hold_expires_at IS NOT NULL)),
    CHECK (status <> 'CONFIRMED' OR (confirmed_at IS NOT NULL AND ticket_code IS NOT NULL)),
    CHECK (status NOT IN ('EXPIRED', 'RELEASED') OR ended_at IS NOT NULL)
);
-- THE anti-oversell / anti-double-allocation guarantees. Any concurrent attempt
-- to give one seat (or one entry) a second active allocation fails with a
-- unique violation, whatever the application code does.
CREATE UNIQUE INDEX allocations_active_seat_uq  ON allocations (seat_id)  WHERE status IN ('HELD', 'CONFIRMED');
CREATE UNIQUE INDEX allocations_active_entry_uq ON allocations (entry_id) WHERE status IN ('HELD', 'CONFIRMED');
CREATE INDEX allocations_held_expiry_idx  ON allocations (event_id, hold_expires_at) WHERE status = 'HELD';
CREATE INDEX allocations_event_status_idx ON allocations (event_id, status);
CREATE INDEX allocations_seat_idx         ON allocations (seat_id);
CREATE INDEX allocations_entry_idx        ON allocations (entry_id);

-- Allocation state machine:
--   HELD      -> CONFIRMED | EXPIRED | RELEASED
--   CONFIRMED -> RELEASED (admin action)
--   EXPIRED, RELEASED are terminal.
CREATE FUNCTION allocations_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.event_id <> OLD.event_id OR NEW.entry_id <> OLD.entry_id OR NEW.seat_id <> OLD.seat_id THEN
        RAISE EXCEPTION 'allocations: event/entry/seat are immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
           (OLD.status = 'HELD'      AND NEW.status IN ('CONFIRMED', 'EXPIRED', 'RELEASED'))
        OR (OLD.status = 'CONFIRMED' AND NEW.status = 'RELEASED')
    ) THEN
        RAISE EXCEPTION 'allocations: illegal status transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;
CREATE TRIGGER allocations_guard BEFORE UPDATE ON allocations
    FOR EACH ROW EXECUTE FUNCTION allocations_guard();

-- ---------------------------------------------------------------- idempotency
-- Stored responses for Idempotency-Key replays. The row is written in the same
-- transaction as the operation it records, so a key exists iff the operation
-- committed. Admin requests use the nil UUID as user_id.
CREATE TABLE idempotency_keys (
    key           text NOT NULL CHECK (length(key) BETWEEN 1 AND 200),
    user_id       uuid NOT NULL,
    endpoint      text NOT NULL,
    request_hash  text NOT NULL,
    status_code   integer NOT NULL,
    response_body jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (key, user_id, endpoint)
);

-- ---------------------------------------------------------------- audit log
-- Hash-chained per event (see DRAW_SPEC.md for the exact encoding). created_at
-- has no default: the app supplies it because it is part of the hashed bytes.
CREATE TABLE audit_log (
    seq        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id   uuid NULL,
    type       text NOT NULL CHECK (length(type) BETWEEN 1 AND 64),
    payload    jsonb NOT NULL DEFAULT '{{}}',
    prev_hash  text NOT NULL CHECK (prev_hash ~ {HEX64}),
    hash       text NOT NULL UNIQUE CHECK (hash ~ {HEX64}),
    created_at timestamptz NOT NULL
);
CREATE INDEX audit_log_event_seq_idx ON audit_log (event_id, seq);

-- Append-only. The only escape hatch is the dev reset endpoint, which sets the
-- transaction-local flag fairdrop.allow_audit_reset = 'on' to DELETE an event's
-- rows (and logs that it did so). In production also REVOKE UPDATE, DELETE,
-- TRUNCATE on audit_log from the application role (see docs/SCHEMA.md).
CREATE FUNCTION audit_log_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('fairdrop.allow_audit_reset', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_log is append-only (% blocked)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_guard();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_guard();
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS audit_log;
DROP FUNCTION IF EXISTS audit_log_guard();
DROP TABLE IF EXISTS idempotency_keys;
DROP TABLE IF EXISTS allocations;
DROP FUNCTION IF EXISTS allocations_guard();
DROP TABLE IF EXISTS entries;
DROP FUNCTION IF EXISTS entries_guard();
DROP TABLE IF EXISTS seats;
DROP TABLE IF EXISTS event_secrets;
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS users;
DROP FUNCTION IF EXISTS fd_now();
DROP TABLE IF EXISTS dev_clock;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
