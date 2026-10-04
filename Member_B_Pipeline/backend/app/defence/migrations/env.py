"""Alembic environment for the `defence` schema.

* Own version table (defence.alembic_version) so A's migrations never see ours.
* A session-level advisory lock serialises concurrent starts: several replicas
  booting at once must not race on CREATE SCHEMA / the version table.
* The URL arrives via config.attributes["url"] (not the .ini) so passwords with
  '%' or other special characters are never run through ConfigParser interpolation.
"""
from alembic import context
from sqlalchemy import create_engine, pool, text

LOCK_ID = 727002


def run_migrations_online() -> None:
    url = context.config.attributes["url"]
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": LOCK_ID})
        try:
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS defence"))
            conn.commit()
            context.configure(
                connection=conn,
                target_metadata=None,
                version_table="alembic_version",
                version_table_schema="defence",
            )
            with context.begin_transaction():
                context.run_migrations()
            conn.commit()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": LOCK_ID})
            conn.commit()


run_migrations_online()
