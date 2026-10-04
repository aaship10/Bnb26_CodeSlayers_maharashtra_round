"""CAPTCHA waivers (accessible fallback)

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE defence.waivers (
            event_id   uuid NOT NULL,
            user_id    uuid NOT NULL,
            reason     text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (event_id, user_id)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE defence.waivers;")
