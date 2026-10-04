"""decision log

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Analytics, not state: written asynchronously in batches. A crash can lose the last unflushed batch.
    # Deliberately no column for ground truth (users.sim_label belongs to Member C and is never read here).
    op.execute(
        """
        CREATE TABLE defence.decisions (
            id        bigserial PRIMARY KEY,
            event_id  uuid NOT NULL,
            user_id   uuid NOT NULL,
            ts        timestamptz NOT NULL,
            action    text NOT NULL CHECK (action IN ('ALLOW', 'REJECT', 'CHALLENGE')),
            weight    real,                      -- ALLOW only: 1.0 / 0.5 / 0.25
            score     real,                      -- risk score, NULL when the risk engine is off
            signals   jsonb,                     -- the explainable breakdown
            layer     text,                      -- which layer produced a REJECT / CHALLENGE
            ip        inet,
            device    text,
            reason    text NOT NULL DEFAULT ''
        );
        CREATE INDEX decisions_event_idx ON defence.decisions (event_id, id);
        CREATE INDEX decisions_user_idx  ON defence.decisions (event_id, user_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE defence.decisions;")
