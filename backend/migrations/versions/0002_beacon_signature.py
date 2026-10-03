"""Store the drand signature next to the randomness; add fd_clock_now().

For drand quicknet, randomness = SHA-256(signature); keeping the signature lets
anyone re-verify the beacon value against drand's public key.

Revision ID: 0002_beacon_signature
Revises: 0001_initial
Create Date: 2026-10-03
"""
from alembic import op

revision = "0002_beacon_signature"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE events ADD COLUMN beacon_signature text NULL "
        "CHECK (beacon_signature ~ '^[0-9a-f]+$')"
    )
    # Wall-clock variant of fd_now() (same dev offset). Used when a timestamp must
    # be taken AFTER a lock was acquired, e.g. closing the entry window: once the
    # event row is locked FOR UPDATE every committed entry has
    # entered_at <= fd_clock_now(), so closes_at := fd_clock_now() keeps I10 true.
    op.execute("""
        CREATE FUNCTION fd_clock_now() RETURNS timestamptz
        LANGUAGE sql VOLATILE AS $$
            SELECT clock_timestamp() + make_interval(
                secs => COALESCE((SELECT offset_seconds FROM dev_clock WHERE id), 0))
        $$
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION fd_clock_now()")
    op.execute("ALTER TABLE events DROP COLUMN beacon_signature")
