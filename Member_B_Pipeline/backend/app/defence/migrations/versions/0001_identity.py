"""identity tables

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No foreign key to public.users on purpose: that table belongs to Member A and its
    # migrations must never be coupled to ours. user_id is a soft reference.
    op.execute(
        """
        CREATE TABLE defence.identities (
            user_id          uuid PRIMARY KEY,
            email_canonical  text NOT NULL UNIQUE,   -- aliases (+tags, gmail dots) collapse here
            email_original   text NOT NULL,          -- as typed at first registration
            email_domain     text NOT NULL,
            email_pattern    text,                   -- digit-normalised skeleton, NULL if not clusterable
            email_flags      jsonb NOT NULL DEFAULT '{}'::jsonb,
            display_name     text NOT NULL,
            registration_ip  inet,
            registration_subnet text,
            device_id        text,
            user_agent_hash  text,
            registered_at    timestamptz NOT NULL,   -- first OTP request
            verified_at      timestamptz NOT NULL,   -- account creation
            otp_latency_ms   integer NOT NULL
        );
        CREATE INDEX identities_device_idx   ON defence.identities (device_id) WHERE device_id IS NOT NULL;
        CREATE INDEX identities_ip_idx       ON defence.identities (registration_ip);
        CREATE INDEX identities_subnet_idx   ON defence.identities (registration_subnet);
        CREATE INDEX identities_pattern_idx  ON defence.identities (email_pattern, registered_at) WHERE email_pattern IS NOT NULL;
        CREATE INDEX identities_verified_idx ON defence.identities (verified_at);

        CREATE TABLE defence.pending_registrations (
            email_canonical  text PRIMARY KEY,
            email_original   text NOT NULL,
            email_domain     text NOT NULL,
            email_pattern    text,
            email_flags      jsonb NOT NULL DEFAULT '{}'::jsonb,
            display_name     text NOT NULL,
            otp_hash         text NOT NULL,          -- HMAC-SHA256(pepper, email || otp), never the OTP
            attempts         integer NOT NULL DEFAULT 0,
            requested_at     timestamptz NOT NULL,
            expires_at       timestamptz NOT NULL,
            registration_ip  inet,
            registration_subnet text,
            device_id        text,
            user_agent_hash  text
        );
        CREATE INDEX pending_pattern_idx ON defence.pending_registrations (email_pattern, requested_at) WHERE email_pattern IS NOT NULL;
        CREATE INDEX pending_expires_idx ON defence.pending_registrations (expires_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE defence.pending_registrations; DROP TABLE defence.identities;")
